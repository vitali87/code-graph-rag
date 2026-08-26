---
description: "Add support for new programming languages to Code-Graph-RAG using Tree-sitter grammars."
---

# Adding Languages

Code-Graph-RAG makes it easy to add support for any language that has a Tree-sitter grammar. The system automatically handles grammar compilation and integration.

!!! warning
    While you can add languages yourself, we recommend waiting for official full support to ensure optimal parsing quality, comprehensive feature coverage, and robust integration. [Submit a language request](https://github.com/vitali87/code-graph-rag/issues) if you need a specific language supported.

## Quick Start

Use the built-in language management tool:

```bash
cgr language add-grammar <language-name>
```

Examples:

```bash
cgr language add-grammar c-sharp
cgr language add-grammar php
cgr language add-grammar ruby
cgr language add-grammar kotlin
```

## Custom Grammar Repositories

For languages hosted outside the standard tree-sitter organisation:

```bash
cgr language add-grammar --grammar-url https://github.com/custom/tree-sitter-mylang
```

## What Happens Automatically

When you add a language, the tool automatically:

1. **Downloads the Grammar**: Clones the tree-sitter grammar repository as a git submodule
2. **Detects Configuration**: Auto-extracts language metadata from `tree-sitter.json`
3. **Analyses Node Types**: Automatically identifies AST node types for functions/methods, classes/structs, modules/files, and function calls from `node-types.json`
4. **Updates Configuration**: Adds the language to `codebase_rag/language_spec.py`
5. **Enables Parsing**: Makes the language available for codebase analysis (the grammar itself is compiled on first use by the parser loader)

## What does NOT happen automatically

Everything above is derived from the grammar's own `tree-sitter.json` and
`node-types.json`. Anything that depends on what the language *means* is not,
and cannot be, inferred from those files.

A grammar added this way gets **definitions and calls**. Everything below the
line needs language-specific code that someone has written:

| Derived from the grammar | Needs language-specific code |
|---|---|
| Functions, methods, classes | Inheritance (`extends`, `with`, mixins) |
| Modules and files | Import resolution and aliasing |
| Call sites | Qualified-name construction rules |

That code does not have to be a `LanguageHandler`. C# and Dart use the base
handler and still emit inheritance and import edges, through dedicated
extraction paths (`split_csharp_bases` and `extract_dart_parent_classes` in
`class_ingest/parent_extraction.py`; `_parse_csharp_imports` and
`_parse_dart_imports` in `import_processor.py`). A handler is one place such
code lives, not the only one.

What matters is that **somebody wrote it for that language**. `add-grammar`
does not, and cannot: the shape of an `extends` clause or an import alias is
not in `node-types.json`.

Measured example, since fixed: Scala **used to be** in exactly this state —
node types wired, no language-specific extraction. It produced classes,
objects, traits, methods, modules and CALLS edges, and **no inheritance edges
at all**, because `extends A with B` is a grammar production nothing had been
taught to read. Its imports resolved to nothing for every form.

Closing those gaps meant reading the grammar and deciding what the constructs
mean — that `with` introduces mixins, that `import a.{B => C}` binds `C` and
not `B`. None of it is in `node-types.json`, which is why the gap existed for
as long as it did and why nothing reported it.

**The failure is silent.** A partially-supported language produces a graph,
not an error. Queries return fewer results rather than reporting that a
relationship kind is missing, so nothing tells you the graph is thinner than
the code.

So treat `add-grammar` as *"parse this language"*, not *"support this
language"*. If you need inheritance or import edges, someone has to write the
language-specific extraction — in a handler under
`codebase_rag/parsers/handlers/`, or in the per-language branches of
`class_ingest/parent_extraction.py` and `import_processor.py`, as C# and Dart
do.

The [support matrix](../architecture/language-support.md) is the authority on
what each shipped language actually provides; the presence or absence of a
handler is not. For compiler-backed facts on top of the Tree-sitter
backbone, see [Adding a Language Frontend](../architecture/language-frontends.md).

## Example: Adding C# Support

```bash
$ cgr language add-grammar c-sharp
Search: Using default tree-sitter URL: https://github.com/tree-sitter/tree-sitter-c-sharp
OK Submodule added at: grammars/tree-sitter-c-sharp
Auto-detected extensions: ['.cs']
Functions: ['destructor_declaration', 'method_declaration', 'constructor_declaration']
Classes: ['struct_declaration', 'enum_declaration', 'interface_declaration', 'class_declaration']
Modules: ['compilation_unit', 'file_scoped_namespace_declaration', 'namespace_declaration']
Calls: ['invocation_expression']
OK Language 'c-sharp' added
Note: Updated codebase_rag/language_spec.py
```

## Managing Languages

```bash
cgr language list-languages

cgr language remove-language <language-name>
```

## Language Configuration

Each language is defined in the `LANGUAGE_SPECS` dict in `codebase_rag/language_spec.py`:

```python
"language-name": LanguageSpec(
    language="language-name",
    file_extensions=[".ext1", ".ext2"],
    function_node_types=["function_declaration", "method_declaration"],
    class_node_types=["class_declaration", "struct_declaration"],
    module_node_types=["compilation_unit", "source_file"],
    call_node_types=["call_expression", "method_invocation"],
),
```

## Troubleshooting

**Grammar not found**: Use a custom URL if the automatic URL doesn't work:

```bash
cgr language add-grammar --grammar-url https://github.com/custom/tree-sitter-mylang
```

**Version incompatibility**: If you get "Incompatible Language version" errors:

```bash
uv add tree-sitter@latest
```

**Missing node types**: The tool automatically detects common node patterns, but you can manually adjust the configuration in `language_spec.py` if needed.
