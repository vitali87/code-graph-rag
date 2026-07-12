# (H) C# tree-sitter node types and field names (tree-sitter-c-sharp).

# (H) Compilation unit is the file root. It is a FQN scope so a file-scoped
# (H) namespace (a SIBLING of the declarations it governs, not their ancestor)
# (H) can be folded into every type's qn via _csharp_get_name. See that resolver.
TS_CSHARP_COMPILATION_UNIT = "compilation_unit"

# (H) Namespace forms: block `namespace N { ... }` nests declarations under a
# (H) declaration_list child (ordinary ancestor scope); file-scoped
# (H) `namespace N;` does not (handled via the compilation_unit shim).
TS_CSHARP_NAMESPACE_DECLARATION = "namespace_declaration"
TS_CSHARP_FILE_SCOPED_NAMESPACE_DECLARATION = "file_scoped_namespace_declaration"
# (H) A type/member body; a block namespace also nests its top-level types under
# (H) one. Used to tell a top-level type (default `internal`) from a nested type
# (H) or member (default `private`) for export detection.
TS_CSHARP_DECLARATION_LIST = "declaration_list"

# (H) Type declarations -> Class nodes.
TS_CSHARP_CLASS_DECLARATION = "class_declaration"
TS_CSHARP_STRUCT_DECLARATION = "struct_declaration"
# (H) `record`, `record struct`, and `record class` all parse as
# (H) record_declaration (the struct/class kind is a keyword child), so there is
# (H) no separate record_struct_declaration node in tree-sitter-c-sharp 0.23.5.
TS_CSHARP_RECORD_DECLARATION = "record_declaration"
TS_CSHARP_INTERFACE_DECLARATION = "interface_declaration"
TS_CSHARP_ENUM_DECLARATION = "enum_declaration"

# (H) Member declarations -> Function/Method nodes.
TS_CSHARP_METHOD_DECLARATION = "method_declaration"
TS_CSHARP_CONSTRUCTOR_DECLARATION = "constructor_declaration"
TS_CSHARP_DESTRUCTOR_DECLARATION = "destructor_declaration"
TS_CSHARP_LOCAL_FUNCTION_STATEMENT = "local_function_statement"
TS_CSHARP_OPERATOR_DECLARATION = "operator_declaration"
TS_CSHARP_CONVERSION_OPERATOR_DECLARATION = "conversion_operator_declaration"
TS_CSHARP_PROPERTY_DECLARATION = "property_declaration"

# (H) Base spec: `class C : Base, IShape` / `interface I : IOther` /
# (H) `enum E : byte`. A single base_list lumps the base class and interfaces
# (H) together (unlike Java's separate superclass/super_interfaces clauses), so
# (H) the split is heuristic. base_list is unique to C# among the grammars, so
# (H) its presence identifies a C# type node without a language argument.
TS_CSHARP_BASE_LIST = "base_list"
# (H) A base type may be a bare `identifier`, a `generic_name` (`List<int>` ->
# (H) strip type args to the identifier), a `qualified_name` (`System.Exception`),
# (H) a record positional base `primary_constructor_base_type` (`Animal(Name)`),
# (H) or a `predefined_type` (an enum's underlying integral type -> not a base).
TS_CSHARP_GENERIC_NAME = "generic_name"
TS_CSHARP_PRIMARY_CONSTRUCTOR_BASE_TYPE = "primary_constructor_base_type"
TS_CSHARP_PREDEFINED_TYPE = "predefined_type"

# (H) A `modifier` child wraps a single keyword (`public`, `override`, `new`,
# (H) `virtual`). Override detection reads it to tell a real override
# (H) (`override`) from an explicit `new` hide (which must not become OVERRIDES).
TS_CSHARP_MODIFIER = "modifier"
TS_CSHARP_MODIFIER_OVERRIDE = "override"
# (H) A type split across files carries `partial` on every part; parts with the
# (H) same namespace-qualified name are one logical type, unified for member and
# (H) base resolution (see csharp_partial_groups).
TS_CSHARP_MODIFIER_PARTIAL = "partial"

# (H) Visibility modifiers that make a type/member external API surface (seed
# (H) dead-code roots). `protected internal` is two separate modifier children.
TS_CSHARP_MODIFIER_PUBLIC = "public"
TS_CSHARP_MODIFIER_INTERNAL = "internal"
TS_CSHARP_MODIFIER_PROTECTED = "protected"

# (H) Parameter shapes for the method-qn signature. Each `parameter` exposes a
# (H) `type` field; a `params object[]` tail is an unwrapped `array_type` child
# (H) of the parameter_list (grammar quirk), captured directly.
TS_CSHARP_PARAMETER = "parameter"
TS_CSHARP_ARRAY_TYPE = "array_type"

# (H) Local/field declarations for type inference. A local is a
# (H) variable_declaration (type field + variable_declarator[s]); `var` makes the
# (H) type field an implicit_type, so the type is inferred from the initializer.
# (H) A field_declaration wraps a variable_declaration; a property_declaration
# (H) exposes `type` and `name` fields directly.
TS_CSHARP_VARIABLE_DECLARATION = "variable_declaration"
TS_CSHARP_VARIABLE_DECLARATOR = "variable_declarator"
TS_CSHARP_IMPLICIT_TYPE = "implicit_type"
TS_CSHARP_FIELD_DECLARATION = "field_declaration"

# (H) Call forms. A member call `recv.Method(...)` is an invocation_expression
# (H) whose `function` field is a member_access_expression (`expression` receiver
# (H) + `name` method); `this` is its own node type; args are `argument` nodes.
TS_CSHARP_INVOCATION_EXPRESSION = "invocation_expression"
TS_CSHARP_OBJECT_CREATION_EXPRESSION = "object_creation_expression"
TS_CSHARP_MEMBER_ACCESS_EXPRESSION = "member_access_expression"
TS_CSHARP_FIELD_EXPRESSION = "expression"
TS_CSHARP_THIS = "this"
TS_CSHARP_ARGUMENT = "argument"

# (H) Nested scopes that own their own locals; the variable-type walk stops at
# (H) these so a lambda/local-function local cannot leak into (or shadow) the
# (H) enclosing method's type map.
TS_CSHARP_NESTED_SCOPE_TYPES = (
    "lambda_expression",
    "anonymous_method_expression",
    "local_function_statement",
)

# (H) Import form: `using System;`, `using X = Y;`, `global using System.Linq;`.
TS_CSHARP_USING_DIRECTIVE = "using_directive"

# (H) The name node inside a using directive: a dotted `qualified_name` or a bare
# (H) `identifier` (both the imported path and, in the alias form, the alias).
TS_CSHARP_QUALIFIED_NAME = "qualified_name"
TS_CSHARP_IDENTIFIER = "identifier"

# (H) Field names used with child_by_field_name.
TS_CSHARP_FIELD_NAME = "name"
TS_CSHARP_FIELD_OPERATOR = "operator"
TS_CSHARP_FIELD_TYPE = "type"

# (H) Operator/conversion-operator declarations expose no `name` field; a stable
# (H) synthetic name is built from these prefixes so the node still gets a qn.
TS_CSHARP_OPERATOR_NAME_PREFIX = "operator_"
TS_CSHARP_DESTRUCTOR_NAME_PREFIX = "~"
