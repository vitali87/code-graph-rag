# ast-grep language patterns

Basic structural support for a language that has **no tree-sitter `LanguageSpec`**
in cgr can be added with a single YAML file here, instead of a hand-written
tree-sitter traversal. The `AstGrepTier` (`../ast_grep_tier.py`) loads every
`*.yaml` in this directory and, for files whose extension matches, emits
`Module`, `Function`, and `Class` nodes plus `DEFINES` and `IMPORTS`
relationships, using [ast-grep](https://ast-grep.github.io/) patterns.

This is a **basic** tier: names are flat (no nested-namespace qualification) and
there is no call-graph (`CALLS`) resolution. Languages that need that get a full
tree-sitter `LanguageSpec`. The tier is active only when the `ast-grep` extra is
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

`extensions` and `ast_grep_id` are required; the pattern lists are optional.

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
