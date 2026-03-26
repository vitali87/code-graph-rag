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

For languages hosted outside the standard tree-sitter organization:

```bash
cgr language add-grammar --grammar-url https://github.com/custom/tree-sitter-mylang
```

## What Happens Automatically

When you add a language, the tool automatically:

1. **Downloads the Grammar**: Clones the tree-sitter grammar repository as a git submodule
2. **Detects Configuration**: Auto-extracts language metadata from `tree-sitter.json`
3. **Analyzes Node Types**: Automatically identifies AST node types for functions/methods, classes/structs, modules/files, and function calls
4. **Compiles Bindings**: Builds Python bindings from the grammar source
5. **Updates Configuration**: Adds the language to `codebase_rag/language_config.py`
6. **Enables Parsing**: Makes the language immediately available for codebase analysis

## Example: Adding C# Support

```bash
$ cgr language add-grammar c-sharp
Using default tree-sitter URL: https://github.com/tree-sitter/tree-sitter-c-sharp
Adding submodule from https://github.com/tree-sitter/tree-sitter-c-sharp...
Successfully added submodule at grammars/tree-sitter-c-sharp
Auto-detected language: c-sharp
Auto-detected file extensions: ['cs']
Auto-detected node types:
Functions: ['destructor_declaration', 'method_declaration', 'constructor_declaration']
Classes: ['struct_declaration', 'enum_declaration', 'interface_declaration', 'class_declaration']
Modules: ['compilation_unit', 'file_scoped_namespace_declaration', 'namespace_declaration']
Calls: ['invocation_expression']

Language 'c-sharp' has been added to the configuration!
Updated codebase_rag/language_config.py
```

## Managing Languages

```bash
cgr language list-languages

cgr language remove-language <language-name>
```

## Language Configuration

Each language is defined in `codebase_rag/language_config.py`:

```python
"language-name": LanguageConfig(
    name="language-name",
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

**Missing node types**: The tool automatically detects common node patterns, but you can manually adjust the configuration in `language_config.py` if needed.
