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
| Module | DEFINES | Class, Function |
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
