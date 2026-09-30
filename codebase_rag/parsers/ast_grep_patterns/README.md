# ast-grep language patterns

Basic structural support for a language that has **no tree-sitter `LanguageSpec`**
in cgr can be added with a single YAML file here, instead of a hand-written
tree-sitter traversal. The `AstGrepTier` (`../ast_grep_tier.py`) loads every
`*.yaml` in this directory and, for files whose extension matches, emits
`Module`, `Function`, and `Class` nodes plus `DEFINES` and `IMPORTS`
relationships, using [ast-grep](https://ast-grep.github.io/) patterns.

This is a **basic** tier: there is no call-graph (`CALLS`) resolution, and names
are flat (`<module>.<name>`) unless a config sets `scoped_names` (see below).
Languages that need calls get a full tree-sitter `LanguageSpec`. The tier is active only when the `ast-grep` extra is
installed (`pip install code-graph-rag[ast-grep]`); otherwise it is a no-op.

## Config format

```yaml
language: ruby          # human-readable name (documentation only)
ast_grep_id: ruby       # ast-grep language id (see AST_GREP_LANGUAGES)
extensions:             # file extensions routed to this config
  - ".rb"
functions:              # patterns whose match becomes a Function node
  - "def self.$NAME"
  - "def $NAME"
classes:                # patterns whose match becomes a Class node
  - "class $NAME"
  - "module $NAME"
imports:                # patterns whose match becomes an IMPORTS edge
  - "require $PATH"
  - "require_relative $PATH"
```

`extensions` and `ast_grep_id` are required; the rule lists are optional.

## Scoped names

With flat names every definition is `<module>.<name>`, so two definitions of
one name in a file are one node. That is right where a repeated name is one
function (an Elixir multi-clause `def`, a Haskell multi-equation function) and
wrong where it is two (overloads, same-named methods of different types).
`scoped_names: true` names each definition the way the tree-sitter tier names
the same shape, so a language keeps its qualified names if it later moves to
that tier:

| Shape | Qualified name | Defined by |
|---|---|---|
| function in a type | `<module>.<Type>.<name>` (a `Method`) | the type, `DEFINES_METHOD` |
| function or type in a function | `<module>.<function>.<name>` | the function, `DEFINES` |
| type in a type | `<module>.<Outer>.<Inner>` | the module, `DEFINES` |
| second definition of a qualified name | `<qn>@<line>` (`<qn>@<line>_<col>` for a same-line twin) | as above |

Two keys attach members to a type declared elsewhere, the way a Rust
`impl T` block or a Go receiver method does. Both need `scoped_names`:

```yaml
type_extensions:        # declarations that add members to the type they name
  - kind: class_declaration
    has_child: extension           # swift `extension T { }`
functions:
  - kind: function_declaration
    receiver_child: receiver_type  # kotlin `fun T.f()`
```

An extension emits no node of its own. Its members are `<module>.<T>.<name>`,
defined by T's `Class` when the file declares T and by the module otherwise.
Generic arguments and `?` are dropped from the type (`List<T>` -> `List`).

## Rule forms

Each entry in `functions`, `classes` and `imports` is either a **pattern**
(a plain string, as above) or a **kind rule** (a mapping). A rule must set
exactly one of `pattern` or `kind`.

```yaml
functions:
  - "def $NAME"                  # pattern rule
  - kind: function_declaration   # kind rule
```

Prefer a **kind rule** when modifiers or keywords can precede the construct.
A pattern is a fixed shape, so `fun $NAME` matches `fun f()` but *not*
`private suspend fun f()`; both are the same `function_declaration` node, so
one kind rule covers every modifier combination. Prefer a **pattern** when the
language has no dedicated node for the construct (Elixir definitions are all
generic `call` nodes) or when the value you want is a literal rather than an
identifier (a Solidity import path).

Kind rules take the name from the node's `name` field, falling back to its
first identifier-like child. Three optional keys handle the rest:

| Key | Applies to | Effect |
|---|---|---|
| `name_child` | `kind` | take the name from this child kind instead of the default lookup |
| `has_child` | `kind` | skip matches with no child of this kind |
| `name_head` | `pattern` | keep only the leading identifier of the capture |
| `receiver_child` | `kind` | name the declaration under the type this child names (needs `scoped_names`) |

`has_child` disambiguates a node type that covers several concepts. A Nix
`binding` is a function only when its value is a `function_expression`;
without the guard, every attribute in a set would be emitted as a Function.
Likewise a Haskell type signature (`speak :: a -> String`) parses as a
`function` node, so requiring a `match` child keeps the type variable `a`
out of the graph.

`name_head` trims a capture down to the bare name. Elixir's zero-arg and
guarded defs match no parenthesised pattern, so the do-block fallback
captures `guarded(x) when is_integer(x)`; `name_head` reduces it to `guarded`.

## Metavariable conventions

- Definition patterns (`functions`, `classes`) must capture the name as **`$NAME`**.
- Import patterns must capture the imported path as **`$PATH`** (surrounding
  quotes are stripped automatically).

## Ordering

Patterns are tried in order and **the first pattern to match a source line
claims it**. Put specific patterns before general ones so, for example,
`def self.$NAME` (captures `build`) wins over `def $NAME` (would capture `self`)
for `def self.build`.

## Testing a new language

Write patterns against a snippet first:

```python
from ast_grep_py import SgRoot

root = SgRoot(source, "ruby").root()
for node in root.find_all(pattern="def $NAME"):
    print(node.get_match("NAME").text(), node.range().start.line + 1)
```

Then add an end-to-end test mirroring `tests/test_ast_grep_tier.py`.
