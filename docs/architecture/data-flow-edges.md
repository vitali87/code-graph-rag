---
description: "How READS_FROM, WRITES_TO, and FLOWS_TO model I/O and value flow, and how to read their edge properties."
---

# I/O and Data-Flow Edges

This page explains the three relationships that model how code touches external
resources and how values move between them: `READS_FROM`, `WRITES_TO`, and
`FLOWS_TO`. For the one-line schema summary see [Graph Schema](graph-schema.md);
this page is the detailed reference.

All three are **opt-in**. They belong to the `io` capture group, which is
excluded from the default capture set, so a default build emits none of them and
does no extra work. Enable them with the `io` capture group (see
[Configuration](../getting-started/configuration.md)).

## The mental model: taint

The design borrows the vocabulary of **taint analysis**, a standard technique in
program analysis. The idea is a drop of dye in water: mark a value where it
enters the program, then follow it wherever it spreads.

- A **source** is where a value enters from the outside world (reading an
  environment variable, a file, a socket). A value read from a source is
  **tainted** — it carries a note about *where it came from*.
- **Propagation** is how that note travels: through assignments, into function
  calls as arguments, and back out through return values.
- A **sink** is where a value leaves for the outside world (writing to standard
  output, to a file, over the network).

"Taint" carries no other meaning here. When the docs say *"`x` is tainted by
`ENV::K`"*, read it as *"`x` holds a value that originated at the `ENV::K`
resource, and the analysis is tracking it until it is written out or
overwritten."* Turning that tracking into graph edges is what lets a single
query answer *does anything from this source reach that sink?*

## Resource nodes

Sources and sinks are represented by synthetic `Resource` nodes. A resource
qualified name has the form `resource::<KIND>::<identity>`:

- `identity` is the static string literal target when one is available (a file
  path, an environment variable name) and `<dynamic>` when the target is not a
  compile-time constant (for example `open(path)` where `path` is a variable,
  or standard streams that have no literal target).
- `KIND` is one of eight values. The table shows what each represents and, for
  the current Python registry, which calls produce it and in which direction.

| `KIND` | Represents | Detected from (Python) | Direction |
|--------|------------|------------------------|-----------|
| `FILE` | A file on disk | `open(...)` and its handle methods (`.read`, `.write`, …); `json.load` / `json.dump` | read + write |
| `ENV` | An environment variable | `os.getenv(...)`, `os.environ.get(...)` | read |
| `NETWORK` | A network endpoint / URL | `requests.get` / `.head`, `urllib.request.urlopen` (read); `requests.post` / `.put` / `.patch` / `.delete` (write) | read + write |
| `DATABASE` | A database connection | `sqlite3.connect(...)` handle methods (`.execute`, `.fetchone`, `.commit`, …) | read + write |
| `SOCKET` | A network socket | `socket.socket(...)` handle methods (`.recv`, `.send`, …) | read + write |
| `STDOUT` | Standard output | `print(...)` | write |
| `STDIN` | Standard input | *(defined in the schema; no Python source registered yet)* | — |
| `STDERR` | Standard error | *(defined in the schema; no Python source registered yet)* | — |

Example: `os.getenv("K")` refers to `resource::ENV::K`; `print(x)` refers to
`resource::STDOUT::<dynamic>`. The registry is extended in
`codebase_rag/parsers/io_access/registry.py`; `STDIN` and `STDERR` exist as
resource kinds but are not emitted until a source or sink for them is added.

## READS_FROM and WRITES_TO

These connect a **callable to a resource** it touches. The direction is decided
by the call and (for file handles) its mode:

| Code | Edge |
|------|------|
| `os.getenv("K")` | `Function -READS_FROM-> Resource(ENV::K)` |
| `print(x)` | `Function -WRITES_TO-> Resource(STDOUT::<dynamic>)` |
| `open("out.txt", "w")` | `Function -WRITES_TO-> Resource(FILE::out.txt)` |
| `open("cfg.yaml")` | `Function -READS_FROM-> Resource(FILE::cfg.yaml)` |

The source of the edge is the **immediate** enclosing scope of the call — the
`Function`, `Method`, or `Module` that directly contains it. A read or write
inside a nested function is attributed to that nested function alone, never
bubbled up to an enclosing function or the module. (This matches how `CALLS` is
attributed, and how `FLOWS_TO` treats nested scopes below.)

## FLOWS_TO

`FLOWS_TO` records **value flow**: that a value moved from one place to another.
It turns provenance questions into plain graph reachability. Every `FLOWS_TO`
edge carries two properties that say *what kind of flow it is* and *how the value
travelled*:

```
FLOWS_TO  ·  <kind>  ·  <via>
   │            │          │
relationship   which of    the channel the
type (always   the three   value crossed
FLOWS_TO)      shapes      through
```

`kind` is the primary category; `via` is the precise channel, present on two of
the three shapes. All three below come from one function body:

```python
def build():
    return os.getenv("K")      # build returns a value read from ENV::K

def forward(v):
    print(v)

def leak():
    x = os.getenv("K")         # x now carries ENV::K
    print(x)                   # shape 1
    t = os.getenv("T")
    forward(t)                 # shape 2
    r = build()
    print(r)                   # shape 3
```

The three `FLOWS_TO` edges that body produces:

<div class="cgr-flow">
  <div class="edge">
    <span class="node res">ENV::K</span>
    <span class="arrow flows"><span class="rel">FLOWS_TO · resource</span></span>
    <span class="node res">STDOUT::&lt;dynamic&gt;</span>
  </div>
  <div class="edge">
    <span class="node code">flow.leak</span>
    <span class="arrow flows"><span class="rel">FLOWS_TO · arg · arg:0</span></span>
    <span class="node code">flow.forward</span>
  </div>
  <div class="edge">
    <span class="node code">flow.build</span>
    <span class="arrow flows"><span class="rel">FLOWS_TO · return · return</span></span>
    <span class="node code">flow.leak</span>
  </div>
  <div class="legend">
    <span><i class="node res" style="padding:1px 6px">resource::…</i> resource node</span>
    <span><i class="node code" style="padding:1px 6px">module.fn</i> code node</span>
    <span>label reads <code>FLOWS_TO · kind · via</code></span>
  </div>
</div>

### Shape 1 — resource to resource (`kind = resource`)

A value read from one resource reaches a write to another within a function
body. No `via`.

```
Resource(ENV::K) -FLOWS_TO {kind: resource}-> Resource(STDOUT::<dynamic>)
```

`x` is read from `ENV::K`, then passed to `print(x)`, which writes `STDOUT`. Both
endpoints are **resource** nodes. This is the leak/provenance answer: a value
from the environment reached standard output.

### Shape 2 — caller to callee (`kind = arg`)

A tainted local value is passed as an argument into a first-party callee. `via`
names the conduit.

```
Function(leak) -FLOWS_TO {kind: arg, via: arg:0}-> Function(forward)
```

`t` (tainted by `ENV::T`) is passed to `forward(t)` as the 0th positional
argument, so `via = arg:0`. A keyword call such as `forward(v=t)` records
`via = kw:v` instead. Both endpoints are **code** nodes; the edge records taint
crossing a call boundary *into* the callee.

### Shape 3 — callee to caller (`kind = return`, `via = return`)

A callee whose return value is tainted flows that value back to its caller.

```
Function(build) -FLOWS_TO {kind: return, via: return}-> Function(leak)
```

`build()` returns `os.getenv("K")`, and `leak` does `r = build()`, so taint
crosses the call boundary *out of* the callee. `via = return` is simply the
channel name. This edge is emitted both when the returned value is assigned
(`r = build()`) and when it is returned directly (`return build()`).

### Which way the arrow points

The `arg` and `return` edges can look confusing side by side, because they point
in opposite directions even though the caller is the same in both:

```
leak -FLOWS_TO {kind: arg}->    forward     (leak passes t INTO forward)
build -FLOWS_TO {kind: return}-> leak        (build hands a value BACK to leak)
```

Both come from `leak`'s body — `leak` is the **caller** in both. The arrow points
opposite ways because the *value* travels opposite ways across the call
boundary. Picture each function as a box with input slots on the front and one
output chute on the back:

```
   t ──▶│ forward │            │ build │──▶ r
        └─────────┘            └───────┘
   value goes IN               value comes OUT
   caller → callee (arg)       callee → caller (return)
```

`leak` operates both boxes: it **pushes** `t` into `forward`'s input slot
(`leak → forward`), and it **catches** what `build`'s chute produces
(`build → leak`).

Note that the assignment in `r = build()` is *not* what flips the direction. A
return value flows out of the callee regardless; the `r =` only gives that
out-flowing value a name so it can be tracked further downstream (which is how
`print(r)` later completes the `ENV::K → STDOUT` resource flow). The rule is
simply:

> Direction follows the value. **In as an argument → caller → callee (`arg`).
> Out as a return → callee → caller (`return`).**

### Reading an edge

Read any `FLOWS_TO` edge as a sentence:

> a value flowed from **left** to **right**, and it was a `<kind>` flow that
> travelled `<via>`.

Keeping `kind` and `via` as properties on a single relationship type means one
query — `MATCH ()-[:FLOWS_TO]->()` — walks the entire data-flow graph, and you
filter on `r.kind` / `r.via` only when you need the detail.

### `kind` values

`kind` is one of exactly three values:

| `kind` | Endpoints | Meaning | `via` |
|--------|-----------|---------|-------|
| `resource` | `Resource` → `Resource` | A value read from one resource reaches a write to another. | *(none)* |
| `arg` | code → code | A tainted value is passed into a callee as an argument. | `arg:<index>` or `kw:<name>` |
| `return` | code → code | A callee's tainted return value flows back to its caller. | `return` |

"code" endpoints are `Module`, `Function`, or `Method` nodes.

### `via` values

`via` names the exact channel the value crossed. It is present only on `arg` and
`return` edges (a `resource` edge has no `via`):

| `via` | Appears on | Meaning |
|-------|-----------|---------|
| `arg:<index>` | `kind = arg` | Passed as a positional argument; `<index>` is the 0-based position, e.g. `arg:0` for the first argument. |
| `kw:<name>` | `kind = arg` | Passed as a keyword argument; `<name>` is the parameter name, e.g. `kw:token` for `forward(token=t)`. |
| `return` | `kind = return` | Handed back through the callee's `return` statement. |

## Propagation and kill rules

Within a function body, taint moves and disappears by these rules:

- **Copy.** `b = a` copies `a`'s taint (and its origin resource) to `b`.
- **Rebind to a new source.** `x = os.getenv("B")` after `x = os.getenv("A")`
  makes `x` carry `ENV::B`; the discarded `ENV::A` no longer flows from `x`.
- **Kill.** Assigning a tainted variable to something clean removes its taint:
  `x = "safe"` or `x = <untainted variable>` means `x` is no longer tracked, so
  a later `print(x)` produces **no** resource flow. The `READS_FROM` /
  `WRITES_TO` edges for the individual calls are still recorded; only the false
  data-flow edge is suppressed.
- **Co-occurrence is not flow.** An unrelated read sitting next to an untainted
  call produces no `FLOWS_TO` edge. Reading `ENV::K` in the same function that
  calls `helper(u)` with an untainted `u` does not connect the two.

## Scope attribution

Each function, method, and nested definition is analysed as its own unit. A
read, write, or flow is attributed to the **immediate** scope that contains it,
never to an enclosing scope. In particular a value tainted inside a nested
function does not leak into the outer function's flow, and the outer function's
own reads and writes are unaffected.

## Opt-in and endpoints

`FLOWS_TO` and its `Resource` endpoints are only produced when the `io` capture
group is enabled. When enabled, the resource endpoints of a `FLOWS_TO` edge are
always ensured as nodes first, so an edge never dangles to a missing node — even
if `READS_FROM` / `WRITES_TO` themselves are filtered out by a narrower capture
selection.

## Example queries

Once a graph is built with the `io` capture group, these Cypher queries answer
the provenance questions the edges are designed for:

```cypher
// Every value that flows from an environment variable to standard output.
MATCH (a:Resource)-[r:FLOWS_TO {kind: 'resource'}]->(b:Resource)
WHERE a.kind = 'ENV' AND b.kind = 'STDOUT'
RETURN a.qualified_name, b.qualified_name;

// Multi-hop reachability: does any source reach any sink across calls?
MATCH p = (src:Resource)-[:FLOWS_TO*1..8]->(dst:Resource)
RETURN p;

// Which callables read a given resource directly.
MATCH (fn)-[:READS_FROM]->(r:Resource {qualified_name: 'resource::ENV::K'})
RETURN fn.qualified_name;
```

## Scope of the current phase

`FLOWS_TO` is intentionally conservative and intra-procedural in this phase:

- Value flow is tracked within a single function body plus one level of
  argument/return hand-off. It is not path-sensitive (a kill on one branch of an
  `if`/`else` drops taint conservatively).
- Sources and sinks are direct I/O calls from the registry; there are no
  `Parameter` nodes and no SSA-level precision.
- The source/sink registry is Python-only in this phase; other languages need
  their own tables before they emit these edges.

These are deliberate ceilings, chosen so the feature is correct and cheap where
it applies rather than broad and noisy.
