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

# (H) A `modifier` child wraps a single keyword token (`public`, `override`,
# (H) `new`, `virtual`). Override detection reads these to tell a real override
# (H) (`override`) from an explicit hide (`new`).
TS_CSHARP_MODIFIER = "modifier"
TS_CSHARP_MODIFIER_OVERRIDE = "override"
TS_CSHARP_MODIFIER_NEW = "new"

# (H) Call forms.
TS_CSHARP_INVOCATION_EXPRESSION = "invocation_expression"
TS_CSHARP_OBJECT_CREATION_EXPRESSION = "object_creation_expression"

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
