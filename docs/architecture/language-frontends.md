---
description: "How to add a compiler-backed language frontend on top of the Tree-sitter backbone."
---

# Adding a Language Frontend

Tree-sitter is the backbone: it parses every supported language and produces the
graph on its own. A **frontend** layers a real compiler or language server on
top of one language, supplying facts Tree-sitter cannot derive — which
declaration a call actually binds to, whether a callee leaves the repository,
what a macro expanded to.

The invariant every frontend preserves: **a missing toolchain degrades to
today's behaviour, never worse.** If the compiler is absent, unavailable, or
slow, the graph is the Tree-sitter graph. A frontend adds precision; it never
subtracts coverage.

## Two protocols, and which one you want

Both live in `codebase_rag/parsers/frontends/protocol.py`.

### `LanguageFrontend` — returns facts

The common case. Your frontend runs a compiler, translates what it learned into
a `SemanticFacts` bundle, and returns it. The graph builder applies the facts
and writes nothing you did not describe.

```python
class LanguageFrontend(Protocol):
    language: SupportedLanguage

    def available(self) -> bool: ...
    def applies(self, repo_path: Path) -> bool: ...
    def run(self, repo_path: Path, files: Sequence[Path]) -> SemanticFacts: ...
```

C#, Go, Java and Python are implemented this way.

### `EmittingFrontend` — writes graph elements directly

For facts that are not *about* Tree-sitter nodes but are nodes and edges in
their own right: expanded macros, `#include` edges, generated members. These
run in a declared phase and write through the ingestor.

```python
class EmittingFrontend(Protocol):
    language: SupportedLanguage
    phase: FrontendPhase

    def available(self) -> bool: ...
    def applies(self, repo_path: Path) -> bool: ...
    def emit(self, ctx: FrontendEmitContext) -> FrontendEmitResult: ...
```

`FrontendPhase` decides when: `BEFORE_DEFINITIONS` for a frontend that emits its
own definition nodes (the definition pass then skips the files it covered), or
`AFTER_DEFINITIONS` for one that attaches to spans Tree-sitter already produced.

**Choose `LanguageFrontend` unless you are emitting nodes.** The two live in
separate registries precisely because they are different jobs, and a fact
provider that reaches for the ingestor is doing something the protocol is
telling it not to do.

## The fact families

`SemanticFacts` is a bundle of independent families. **Every family is optional.**
Fill the ones your compiler models; leave the rest empty. Each is applied
separately, and any site your frontend did not describe falls back to the
Tree-sitter heuristics.

| Family | What it carries |
|---|---|
| `resolved_call_sites` | The declaration a call binds to |
| `external_sites` | Sites proven to leave the repository |
| `base_kinds` | Base types, distinguishing `INHERITS` from `IMPLEMENTS` |
| `implements_pairs` | `IMPLEMENTS` for structurally-typed languages with no base list |
| `arg_flows`, `bind_flows`, `out_writes` | Data-flow facts backing `FLOWS_TO` |
| `partial_groups`, `query_calls` | C#-specific today |

### Three states, not two

The distinction that matters most, and the one most easily got wrong:

- **key in `resolved_call_sites`** — analysed, and it binds *here*;
- **key in `external_sites`** — analysed, and it leaves the repo, so the
  name-trie must **not** fabricate a first-party edge;
- **key in neither** — not analysed. Fall back to the heuristics.

"Found nothing" and "did not look" are different facts, and they are two
separate collections so that a consumer can tell them apart.

### When you cannot resolve to exactly one target

Omit the fact. Do not guess.

`resolved_call_sites` maps one key to one target, so a site with several
plausible bindings has no representation — and that is deliberate. The Python
frontend shows the intended handling:

```python
def _single_resolvable_target(names):
    # Exactly one inferred function/class is a usable fact; anything else
    # (ambiguity, modules, instances) is the ceiling: no fact, never a guess.
    if len(names) != 1:
        return None
```

A site you decline to describe degrades to the heuristic, which is correct. A
site you describe wrongly poisons the graph, and nothing downstream can tell.

This is a known limit rather than an oversight: a frontend that *knows* three
candidates cannot say so, and the trie re-derives them less well. Languages
whose dispatch is genuinely dynamic — Ruby `method_missing`, mixin ancestry,
`noSuchMethod` — will hit it first. Raise it with the concrete case when you do.

## The join key

```python
CallSiteKey = tuple[str, int, int, str]
# (rel_file, name_token_line, name_token_byte_col, simple_name)
```

Two details, both load-bearing:

**The NAME token, not the expression start.** Nested invocations such as
`Make().Handle(x)` share a start position; their name tokens never collide.

**Columns are UTF-8 BYTE offsets.** Tree-sitter is byte-oriented. A compiler
reporting UTF-16 columns (Roslyn) or character columns (Jedi) must re-measure
before building a key, or every join silently misses on any line containing a
non-ASCII character.

That failure mode deserves emphasis, because it has bitten this repository more
than once: the offsets agree exactly until a line contains a character outside
ASCII, and then they diverge by the difference. **An all-ASCII fixture cannot
distinguish byte offsets from character offsets from UTF-16 code units** — all
three are "correct" until they are not. Put a non-ASCII character above an
asserted span in your fixtures, then deliberately break the conversion and
confirm the test fails. If it stays green, the fixture is not positioned to
catch anything.

## Availability and enablement

`available()` must answer "can this actually run", not "is something installed".
A tool present on disk whose runtime is missing is present, executable, and
non-functional — probe by invoking it, and treat any failure as unavailable.

Register a per-language enum following the existing pattern
(`constants/languages.py`), for example `PythonFrontend.HEURISTIC | JEDI`, and
register with `parser_fingerprint.py` so that changing mode or tool version
invalidates the incremental graph. A graph built with one frontend and updated
with another is worse than either.

## Shelling out

Always pass an explicit encoding:

```python
subprocess.run(..., text=True, encoding=cs.ENCODING_UTF8)
```

`text=True` alone decodes with the **locale** encoding — cp1252 on Windows —
while the tool you called almost certainly wrote UTF-8. `test_subprocess_encoding.py`
fails the build if you forget, so this is enforced rather than advisory.

## Checklist

1. Pick `LanguageFrontend` (facts) or `EmittingFrontend` (nodes and edges).
2. Implement `available()` by probing, not by checking for a file.
3. Implement `applies()` — usually "is there a project file for this language".
4. Fill only the fact families your compiler genuinely models.
5. Build keys with byte columns, re-measuring if your tool reports otherwise.
6. Omit rather than guess whenever resolution is ambiguous.
7. Call `register_frontend(...)` or `register_emitting_frontend(...)` at module
   scope, and import the module in `frontends/__init__.py`.
8. Add a per-language enum and a `parser_fingerprint.py` registration.
9. Write fixtures that include a non-ASCII identifier, and prove they fail when
   the offset conversion is broken.
