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

A grammar added this way gets **definitions and calls**. It does not get a
`LanguageHandler`, and several capabilities live there:

| Derived from the grammar | Needs a language handler |
|---|---|
| Functions, methods, classes | Inheritance (`extends`, `with`, mixins) |
| Modules and files | Import resolution and aliasing |
| Call sites | Qualified-name construction rules |

Measured example: Scala shipped with its node types wired and no handler. It
produced classes, objects, traits, methods, modules and CALLS edges — and
**no inheritance edges at all**, because `extends A with B` is a grammar
production nothing had been taught to read. Its imports resolved to nothing
for every form. Both took reading the grammar and deciding what the
constructs mean; neither is in `node-types.json`.

**The failure is silent.** A partially-supported language produces a graph,
not an error. Queries return fewer results rather than reporting that a
relationship kind is missing, so nothing tells you the graph is thinner than
the code.

So treat `add-grammar` as *"parse this language"*, not *"support this
language"*. If you need inheritance or import edges, the language needs a
handler in `codebase_rag/parsers/handlers/` — see
[Adding a Language Frontend](../architecture/language-frontends.md) and the
[support matrix](../architecture/language-support.md) for what each shipped
language actually provides.

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
