---
description: "Knowledge graph schema with node types, relationships, and language-specific AST mappings."
---

# Graph Schema

The knowledge graph uses a unified schema across all supported languages.

## Node Types

| Label | Properties |
|-------|------------|
| Project | `{name: string}` |
| Package | `{qualified_name: string, name: string, path: string, absolute_path: string}` |
| Folder | `{path: string, name: string, absolute_path: string}` |
| File | `{path: string, name: string, extension: string?, absolute_path: string}` |
| Module | `{qualified_name: string, name: string, path: string, absolute_path: string, start_line: int?, end_line: int?}` |
| Class | `{qualified_name: string, name: string, modifiers: list[string], decorators: list[string], path: string, absolute_path: string, start_line: int?, end_line: int?, docstring: string?, is_exported: boolean?}` |
| Function | same as Class, plus `is_macro: boolean?` |
| Method | same as Class, plus `is_property: boolean?, overrides_external: boolean?` |
| Interface | `{qualified_name: string, name: string, path: string, absolute_path: string, modifiers: list[string]?, decorators: list[string]?, start_line: int?, end_line: int?, docstring: string?, is_exported: boolean?}` |
| Enum | same as Interface |
| Type | same as Interface, but `path` and `absolute_path` are optional |
| Union | same as Type |
| ModuleInterface | `{qualified_name: string, name: string, path: string, absolute_path: string, module_type: string}` |
| ModuleImplementation | `{qualified_name: string, name: string, path: string, absolute_path: string, implements_module: string, module_type: string}` |
| ExternalPackage | `{name: string}` |
| ExternalModule | `{qualified_name: string, name: string, path: string}` |
| Resource | `{qualified_name: string, name: string, kind: string}` |
| Pattern | `{qualified_name: string, name: string, message: string, start_line: int, end_line: int, path: string, snippet: string?}` |
| CodeSmell | same as Pattern |
| SecurityIssue | same as Pattern |

`ExternalModule` stands for an imported module that lives outside the repository (a third-party or stdlib target of `IMPORTS`, or a positively-external base class target of `INHERITS`/`IMPLEMENTS`).

`Resource` is a synthetic node standing for an external I/O target (a file, environment variable, network endpoint, database, standard stream, socket). Its `qualified_name` has the form `resource::<KIND>::<identity>`, where `identity` is a static string literal when one is available and `<dynamic>` otherwise, and `kind` is one of `FILE`, `NETWORK`, `DATABASE`, `STDIN`, `STDOUT`, `STDERR`, `ENV`, `SOCKET`. Resource nodes are captured only when the `io` capture group is enabled (see below).

`Pattern`, `CodeSmell`, and `SecurityIssue` are ast-grep finding nodes, captured only when the `findings` capture group is enabled.

## Relationships

| Source | Relationship | Target |
|--------|-------------|--------|
| Project, Package, Folder | CONTAINS_PACKAGE | Package |
| Project, Package, Folder | CONTAINS_FOLDER | Folder |
| Project, Package, Folder | CONTAINS_FILE | File |
| Project, Package, Folder | CONTAINS_MODULE | Module |
| Module, Function, Method, Class | DEFINES | Class, Function, Method, Enum, Interface, Type, Union, Module |
| Class, Interface, Enum, Type, Union | DEFINES_METHOD | Method |
| Module | IMPORTS | Module, ExternalModule |
| Module | EXPORTS | Class, Function |
| Module | EXPORTS_MODULE | ModuleInterface |
| Module | IMPLEMENTS_MODULE | ModuleImplementation |
| Class, Interface, Function | INHERITS | Class, Interface, Function, ExternalModule |
| Class, Enum | IMPLEMENTS | Interface, Class, Enum, ExternalModule |
| Method, Function | OVERRIDES | Method |
| ModuleImplementation | IMPLEMENTS | ModuleInterface |
| Project | DEPENDS_ON_EXTERNAL | ExternalPackage |
| Module, Function, Method | CALLS | Function, Method, Enum, Type |
| Module, Function, Method | REFERENCES | Function, Method, Class |
| Module, Function, Method | INSTANTIATES | Class |
| Module, Function, Method | READS_FROM | Resource |
| Module, Function, Method | WRITES_TO | Resource |
| Module, Function, Method, Resource | FLOWS_TO | Module, Function, Method, Resource |
| Module | IMPLEMENTS_PATTERN | Pattern |
| Module | HAS_SMELL | CodeSmell |
| Module | HAS_VULNERABILITY | SecurityIssue |

`REFERENCES` records a non-call mention of a callable or class (a function passed as a value, a callback stored in a dict). `INSTANTIATES` records a class being constructed. Both belong to the default `calls` capture group. The findings relationships (`IMPLEMENTS_PATTERN`, `HAS_SMELL`, `HAS_VULNERABILITY`) are opt-in with the `findings` capture group.

## I/O and Data-Flow Edges

The `io` capture group (opt-in; excluded from the default capture set) adds three relationships that model how code touches external resources and how values move between them.

`READS_FROM` and `WRITES_TO` connect a callable to a `Resource` it reads from or writes to (for example `os.getenv("K")` reads the `ENV` resource, `print(x)` writes the `STDOUT` resource).

`FLOWS_TO` records intra-procedural value flow, turning provenance questions into graph reachability. It is emitted in three shapes, distinguished by a `kind` edge property:

- **resource → resource** (`kind = resource`): a value read from one resource reaches a write to another within a function body, e.g. `x = os.getenv("K"); print(x)` yields `Resource(ENV::K) -FLOWS_TO-> Resource(STDOUT)`.
- **caller → callee** (`kind = arg`): a tainted local value is passed as an argument to a first-party callee. A `via` edge property names the conduit as `arg:<index>` or `kw:<name>`.
- **callee → caller** (`kind = return`, `via = return`): a callee whose return value is tainted flows that value back to the assignment in its caller.

Taint is propagated through plain `x = y` assignments. `FLOWS_TO` is intentionally conservative and intra-procedural in this phase; a tainted value is only tracked within a single function body plus one level of argument/return hand-off.

See [I/O and Data-Flow Edges](data-flow-edges.md) for the detailed reference: the taint model, propagation and kill rules, the `kind`/`via` edge properties, scope attribution, and example queries.

## Nested Definitions

A function or class defined inside another function or method (a closure or a function-local class) is attached by `DEFINES` to its **enclosing scope**, not flattened onto the Module. So `DEFINES` can originate from a `Function` or `Method` as well as a `Module`. A top-level function or class is still defined by its `Module`.

Methods of classes defined inside function bodies are captured only when `CGR_CAPTURE_LOCAL_DEFINITIONS` is enabled, which is the default (see [Configuration](../getting-started/configuration.md)); function-local *classes* are always captured, and setting the flag to `false` skips their methods.

## Qualified Name Uniqueness

`qualified_name` uniquely identifies each `Function`, `Method`, and `Class` node. When the same qualified name is defined more than once in a module, every definition is kept as a distinct node. This happens with the `if has_x(): ... else: ...` import-fallback idiom, `typing.overload`, and `try/except ImportError` fallbacks.

The first definition keeps the plain dotted qualified name; each later definition is suffixed with `@<start_line>` (for example `pkg.module.store_embedding@161`) so both survive instead of one overwriting the other. The `name` property stays the plain name on every variant.

A `CALLS` edge to a name that has more than one definition links to every variant, since each is a runtime-possible target.

## Macros

Macro definitions map onto the existing `Function` label rather than a dedicated node type, since macros are a cross-language concept (C and C++ `#define`, Rust `macro_rules!`). Macro Function nodes carry `is_macro: true`, macro invocations resolve to their definitions and emit `CALLS` edges, and dead-code analysis treats macros like any function.

Language notes:

- **Rust**: macros and functions live in separate namespaces, so a macro invocation (`write!`) never binds a same-named `fn` and a function call never binds a same-named macro. `#[macro_export]` sets `is_exported` (macros take no `pub`).
- **C/C++** (macro semantics, shared by the libclang-backed modes): compiler builtins, system-header macros, and empty-bodied object-like macros (include guards, feature flags) are not nodes. A macro use inside a function body emits `CALLS` from that function; a use outside any function attributes to the `Module`. A macro whose definition body references another macro emits a macro-to-macro `CALLS` edge, since nested expansions are never reported as individual uses.
- **C/C++ hybrid mode** (the default: `CPP_FRONTEND=hybrid`; `libclang` forces the pure libclang frontend and `treesitter` disables libclang entirely): tree-sitter remains the backbone (every file gets its tree-sitter definitions and calls; nothing is skipped) and libclang layers on only macro `Function` nodes and `#include` `IMPORTS` edges, whose qualified names are identical between the two schemes. Macro uses are attributed to the tightest enclosing tree-sitter definition span after the definition pass, so macro `CALLS` edges join the qualified-name scheme the rest of the graph uses.
- **C# hybrid mode** (the default: `CSHARP_FRONTEND=auto` runs it wherever `dotnet` is on PATH, falling back to pure tree-sitter otherwise; `hybrid`/`roslyn` force it, `treesitter` disables it): tree-sitter remains the backbone and a bundled Roslyn tool (requires `dotnet`) layers on location-keyed semantic facts. Base lists get exact `INHERITS`-vs-`IMPLEMENTS` classification; each invocation site gets the compiler's own overload resolution (argument types, not arity) and extension-method binding, overriding the syntactic heuristics per call; `partial` types merge by symbol identity instead of the directory heuristic; and LINQ query-syntax operators that resolve to first-party methods emit `CALLS` edges tree-sitter cannot see (query syntax has no invocation nodes). Source generators run inside the workspace compilation, so resolution through generated members works, but generated code has no repo file and gets no nodes. Any missing fact degrades to the tree-sitter heuristic for that site.

## Language-Specific AST Mappings

The function- and class-defining AST node types captured per language (auto-generated from the language specs):

<!-- SECTION:language_mappings -->
- **C**: `enum_specifier`, `function_definition`, `struct_specifier`, `union_specifier`
- **C#**: `class_declaration`, `constructor_declaration`, `conversion_operator_declaration`, `destructor_declaration`, `enum_declaration`, `interface_declaration`, `local_function_statement`, `method_declaration`, `operator_declaration`, `property_declaration`, `record_declaration`, `struct_declaration`
- **C++**: `class_specifier`, `declaration`, `enum_specifier`, `field_declaration`, `function_definition`, `lambda_expression`, `struct_specifier`, `template_declaration`, `union_specifier`
- **Dart**: `class_definition`, `constant_constructor_signature`, `constructor_signature`, `enum_declaration`, `extension_declaration`, `extension_type_declaration`, `factory_constructor_signature`, `function_signature`, `getter_signature`, `mixin_declaration`, `setter_signature`
- **Go**: `function_declaration`, `method_declaration`, `type_alias`, `type_spec`
- **Java**: `annotation_type_declaration`, `class_declaration`, `constructor_declaration`, `enum_declaration`, `interface_declaration`, `method_declaration`, `record_declaration`
- **JavaScript**: `arrow_function`, `class`, `class_declaration`, `function_declaration`, `function_expression`, `generator_function_declaration`, `method_definition`
- **Lua**: `function_declaration`, `function_definition`
- **PHP**: `anonymous_function`, `arrow_function`, `class_declaration`, `enum_declaration`, `function_definition`, `interface_declaration`, `method_declaration`, `trait_declaration`
- **Python**: `class_definition`, `function_definition`
- **Rust**: `closure_expression`, `enum_item`, `function_item`, `function_signature_item`, `impl_item`, `macro_definition`, `struct_item`, `trait_item`, `type_item`, `union_item`
- **TypeScript (TSX)**: `abstract_class_declaration`, `arrow_function`, `class`, `class_declaration`, `enum_declaration`, `function_declaration`, `function_expression`, `function_signature`, `generator_function_declaration`, `interface_declaration`, `internal_module`, `method_definition`, `type_alias_declaration`
- **TypeScript**: `abstract_class_declaration`, `arrow_function`, `class`, `class_declaration`, `enum_declaration`, `function_declaration`, `function_expression`, `function_signature`, `generator_function_declaration`, `interface_declaration`, `internal_module`, `method_definition`, `type_alias_declaration`
- **Scala**: `class_definition`, `function_declaration`, `function_definition`, `object_definition`, `trait_definition`
<!-- /SECTION:language_mappings -->
