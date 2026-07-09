---
description: "Knowledge graph schema with node types, relationships, and language-specific AST mappings."
---

# Graph Schema

The knowledge graph uses a unified schema across all supported languages.

## Node Types

| Label | Properties |
|-------|------------|
| Project | `{name: string}` |
| Package | `{qualified_name: string, name: string, path: string}` |
| Folder | `{path: string, name: string}` |
| File | `{path: string, name: string, extension: string}` |
| Module | `{qualified_name: string, name: string, path: string}` |
| Class | `{qualified_name: string, name: string, decorators: list[string]}` |
| Function | `{qualified_name: string, name: string, decorators: list[string]}` |
| Method | `{qualified_name: string, name: string, decorators: list[string]}` |
| Interface | `{qualified_name: string, name: string}` |
| Enum | `{qualified_name: string, name: string}` |
| Type | `{qualified_name: string, name: string}` |
| Union | `{qualified_name: string, name: string}` |
| ModuleInterface | `{qualified_name: string, name: string, path: string}` |
| ModuleImplementation | `{qualified_name: string, name: string, path: string, implements_module: string}` |
| ExternalPackage | `{name: string, version_spec: string}` |

## Relationships

| Source | Relationship | Target |
|--------|-------------|--------|
| Project, Package, Folder | CONTAINS_PACKAGE | Package |
| Project, Package, Folder | CONTAINS_FOLDER | Folder |
| Project, Package, Folder | CONTAINS_FILE | File |
| Project, Package, Folder | CONTAINS_MODULE | Module |
| Module, Function, Method | DEFINES | Class, Function |
| Class | DEFINES_METHOD | Method |
| Module | IMPORTS | Module |
| Module | EXPORTS | Class, Function |
| Module | EXPORTS_MODULE | ModuleInterface |
| Module | IMPLEMENTS_MODULE | ModuleImplementation |
| Class | INHERITS | Class |
| Class | IMPLEMENTS | Interface |
| Method | OVERRIDES | Method |
| ModuleImplementation | IMPLEMENTS | ModuleInterface |
| Project | DEPENDS_ON_EXTERNAL | ExternalPackage |
| Function, Method | CALLS | Function, Method |

## Nested Definitions

A function or class defined inside another function or method (a closure or a function-local class) is attached by `DEFINES` to its **enclosing scope**, not flattened onto the Module. So `DEFINES` can originate from a `Function` or `Method` as well as a `Module`. A top-level function or class is still defined by its `Module`.

Methods and classes defined inside function bodies are captured only when `CGR_CAPTURE_LOCAL_DEFINITIONS` is enabled (see [Configuration](../getting-started/configuration.md)); function-local *classes* are captured by default, but their methods require the flag.

## Qualified Name Uniqueness

`qualified_name` uniquely identifies each `Function`, `Method`, and `Class` node. When the same qualified name is defined more than once in a module, every definition is kept as a distinct node. This happens with the `if has_x(): ... else: ...` import-fallback idiom, `typing.overload`, and `try/except ImportError` fallbacks.

The first definition keeps the plain dotted qualified name; each later definition is suffixed with `@<start_line>` (for example `pkg.module.store_embedding@161`) so both survive instead of one overwriting the other. The `name` property stays the plain name on every variant.

A `CALLS` edge to a name that has more than one definition links to every variant, since each is a runtime-possible target.

## Macros

Macro definitions map onto the existing `Function` label rather than a dedicated node type, since macros are a cross-language concept (C and C++ `#define`, Rust `macro_rules!`). Macro Function nodes carry `is_macro: true`, macro invocations resolve to their definitions and emit `CALLS` edges, and dead-code analysis treats macros like any function.

Language notes:

- **Rust**: macros and functions live in separate namespaces, so a macro invocation (`write!`) never binds a same-named `fn` and a function call never binds a same-named macro. `#[macro_export]` sets `is_exported` (macros take no `pub`).
- **C/C++** (libclang frontend): compiler builtins, system-header macros, and empty-bodied object-like macros (include guards, feature flags) are not nodes. A macro use inside a function body emits `CALLS` from that function; a use outside any function attributes to the `Module`. A macro whose definition body references another macro emits a macro-to-macro `CALLS` edge, since nested expansions are never reported as individual uses.
- **C/C++ hybrid mode** (`CPP_FRONTEND=hybrid`): tree-sitter remains the backbone (every file gets its tree-sitter definitions and calls; nothing is skipped) and libclang layers on only macro `Function` nodes and `#include` `IMPORTS` edges, whose qualified names are identical between the two schemes. Macro uses are attributed to the tightest enclosing tree-sitter definition span after the definition pass, so macro `CALLS` edges join the qualified-name scheme the rest of the graph uses.

## Language-Specific AST Mappings

### C++

- `class_specifier`
- `declaration`
- `enum_specifier`
- `field_declaration`
- `function_definition`
- `lambda_expression`
- `struct_specifier`
- `template_declaration`
- `union_specifier`

### Java

- `annotation_type_declaration`
- `class_declaration`
- `constructor_declaration`
- `enum_declaration`
- `interface_declaration`
- `method_declaration`
- `record_declaration`

### JavaScript

- `arrow_function`
- `class`
- `class_declaration`
- `function_declaration`
- `function_expression`
- `generator_function_declaration`
- `method_definition`

### Lua

- `function_declaration`
- `function_definition`

### Python

- `class_definition`
- `function_definition`

### Rust

- `closure_expression`
- `enum_item`
- `function_item`
- `function_signature_item`
- `impl_item`
- `macro_definition`
- `struct_item`
- `trait_item`
- `type_item`
- `union_item`

### TypeScript

- `abstract_class_declaration`
- `arrow_function`
- `class`
- `class_declaration`
- `enum_declaration`
- `function_declaration`
- `function_expression`
- `function_signature`
- `generator_function_declaration`
- `interface_declaration`
- `internal_module`
- `method_definition`
- `type_alias_declaration`

### C#

- `anonymous_method_expression`
- `class_declaration`
- `constructor_declaration`
- `destructor_declaration`
- `enum_declaration`
- `function_pointer_type`
- `interface_declaration`
- `lambda_expression`
- `local_function_statement`
- `method_declaration`
- `struct_declaration`

### Go

- `function_declaration`
- `method_declaration`
- `type_declaration`

### PHP

- `anonymous_function`
- `arrow_function`
- `class_declaration`
- `enum_declaration`
- `function_definition`
- `function_static_declaration`
- `interface_declaration`
- `trait_declaration`

### Scala

- `class_definition`
- `function_declaration`
- `function_definition`
- `object_definition`
- `trait_definition`
