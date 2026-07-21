# C# tree-sitter node types and field names (tree-sitter-c-sharp).

# Compilation unit is the file root. It is a FQN scope so a file-scoped
# namespace (a SIBLING of the declarations it governs, not their ancestor)
# can be folded into every type's qn via _csharp_get_name. See that resolver.
TS_CSHARP_COMPILATION_UNIT = "compilation_unit"

# Namespace forms: block `namespace N { ... }` nests declarations under a
# declaration_list child (ordinary ancestor scope); file-scoped
# `namespace N;` does not (handled via the compilation_unit shim).
TS_CSHARP_NAMESPACE_DECLARATION = "namespace_declaration"
TS_CSHARP_FILE_SCOPED_NAMESPACE_DECLARATION = "file_scoped_namespace_declaration"
# A type/member body; a block namespace also nests its top-level types under
# one. Used to tell a top-level type (default `internal`) from a nested type
# or member (default `private`) for export detection.
TS_CSHARP_DECLARATION_LIST = "declaration_list"
TS_CSHARP_EXPLICIT_INTERFACE_SPECIFIER = "explicit_interface_specifier"

# Type declarations -> Class nodes.
TS_CSHARP_CLASS_DECLARATION = "class_declaration"
TS_CSHARP_STRUCT_DECLARATION = "struct_declaration"
# `record`, `record struct`, and `record class` all parse as
# record_declaration (the struct/class kind is a keyword child), so there is
# no separate record_struct_declaration node in tree-sitter-c-sharp 0.23.5.
TS_CSHARP_RECORD_DECLARATION = "record_declaration"
TS_CSHARP_INTERFACE_DECLARATION = "interface_declaration"
TS_CSHARP_ENUM_DECLARATION = "enum_declaration"

# A conditional-compilation block wrapping a declaration's attributes
# (`#if SYMBOL [Attr] #endif`) parses as this node, which sits as the leading
# child of the declaration -- so the declaration's start_point is the `#if`
# directive line. The real first token (Roslyn's span start) is the
# attribute_list nested inside it.
TS_CSHARP_PREPROC_IF_IN_ATTR_LIST = "preproc_if_in_attribute_list"
TS_CSHARP_ATTRIBUTE_LIST = "attribute_list"

# Member declarations -> Function/Method nodes.
TS_CSHARP_METHOD_DECLARATION = "method_declaration"
TS_CSHARP_CONSTRUCTOR_DECLARATION = "constructor_declaration"
TS_CSHARP_DESTRUCTOR_DECLARATION = "destructor_declaration"
TS_CSHARP_LOCAL_FUNCTION_STATEMENT = "local_function_statement"
TS_CSHARP_OPERATOR_DECLARATION = "operator_declaration"
TS_CSHARP_CONVERSION_OPERATOR_DECLARATION = "conversion_operator_declaration"
TS_CSHARP_PROPERTY_DECLARATION = "property_declaration"

# The scopes a local function can be declared in (and therefore be
# call-visible from): a method/constructor body or an enclosing local
# function. Used to pin each local function to its HOST so bare-name
# resolution honors C# scoping (a local fn in one overload's body is not
# in scope in a sibling overload).
CSHARP_LOCAL_FN_HOST_TYPES = frozenset(
    {
        TS_CSHARP_METHOD_DECLARATION,
        TS_CSHARP_CONSTRUCTOR_DECLARATION,
        TS_CSHARP_LOCAL_FUNCTION_STATEMENT,
    }
)

# Members whose registered leaf name is synthesized (no usable `name` field,
# or one that collides), routed through csharp.utils.synthesize_method_name.
CSHARP_SYNTHESIZED_NAME_TYPES = frozenset(
    {
        TS_CSHARP_OPERATOR_DECLARATION,
        TS_CSHARP_CONVERSION_OPERATOR_DECLARATION,
        TS_CSHARP_DESTRUCTOR_DECLARATION,
    }
)

# Declaration node types that are grammatically ONLY ever a type member -- C#
# has no top-level method/constructor/operator/property (a real top-level
# function is a local_function_statement, deliberately excluded). When a `#if`
# split truncates a class_declaration node early, tree-sitter detaches the
# following members into the namespace's declaration_list with no class
# ancestor, so the generic is_method_node ancestor walk returns False and they
# would be mislabelled module Functions. This set drives their recovery as
# Methods (function_ingest), by grammar invariant.
CSHARP_MEMBER_ONLY_TYPES = frozenset(
    {
        TS_CSHARP_METHOD_DECLARATION,
        TS_CSHARP_CONSTRUCTOR_DECLARATION,
        TS_CSHARP_DESTRUCTOR_DECLARATION,
        TS_CSHARP_OPERATOR_DECLARATION,
        TS_CSHARP_CONVERSION_OPERATOR_DECLARATION,
        TS_CSHARP_PROPERTY_DECLARATION,
    }
)

# Base spec: `class C : Base, IShape` / `interface I : IOther` /
# `enum E : byte`. A single base_list lumps the base class and interfaces
# together (unlike Java's separate superclass/super_interfaces clauses), so
# the split is heuristic. base_list is unique to C# among the grammars, so
# its presence identifies a C# type node without a language argument.
TS_CSHARP_BASE_LIST = "base_list"
# A base type may be a bare `identifier`, a `generic_name` (`List<int>` ->
# strip type args to the identifier), a `qualified_name` (`System.Exception`),
# a record positional base `primary_constructor_base_type` (`Animal(Name)`),
# or a `predefined_type` (an enum's underlying integral type -> not a base).
TS_CSHARP_GENERIC_NAME = "generic_name"
TS_CSHARP_CAST_EXPRESSION = "cast_expression"
TS_CSHARP_POSTFIX_UNARY_EXPRESSION = "postfix_unary_expression"
TS_CSHARP_IMPLICIT_PARAMETER = "implicit_parameter"
TS_CSHARP_FIELD_TYPE_PARAMETERS = "type_parameters"
TS_CSHARP_TYPE_PARAMETER_LIST = "type_parameter_list"
# The `base` receiver keyword parses as a bare "base" node in this
# grammar version (not "base_expression").
TS_CSHARP_BASE_EXPRESSION = "base"

# object's virtual/universal members: a call to one of these on an UNTYPED
# receiver resolves to System.Object (or a BCL override), so binding it by
# bare name to whatever first-party override happens to exist fabricates
# an edge (exposed by the semantic calls oracle on Polly's
# hide-object-members regions).
CSHARP_OBJECT_VIRTUALS = frozenset(
    {
        "Equals",
        "GetHashCode",
        "GetType",
        "ToString",
        "ReferenceEquals",
        "MemberwiseClone",
    }
)
TS_CSHARP_LAMBDA_EXPRESSION = "lambda_expression"
TS_CSHARP_BLOCK = "block"
TS_CSHARP_PRIMARY_CONSTRUCTOR_BASE_TYPE = "primary_constructor_base_type"
TS_CSHARP_PREDEFINED_TYPE = "predefined_type"

# A `modifier` child wraps a single keyword (`public`, `override`, `new`,
# `virtual`). Override detection reads it to tell a real override
# (`override`) from an explicit `new` hide (which must not become OVERRIDES).
TS_CSHARP_MODIFIER = "modifier"
TS_CSHARP_MODIFIER_OVERRIDE = "override"
# A type split across files carries `partial` on every part; parts with the
# same namespace-qualified name are one logical type, unified for member and
# base resolution (see csharp_partial_groups).
TS_CSHARP_MODIFIER_PARTIAL = "partial"

# Visibility modifiers that make a type/member external API surface (seed
# dead-code roots). `protected internal` is two separate modifier children.
TS_CSHARP_MODIFIER_PUBLIC = "public"
TS_CSHARP_MODIFIER_INTERNAL = "internal"
TS_CSHARP_MODIFIER_PROTECTED = "protected"

# Parameter shapes for the method-qn signature. Each `parameter` exposes a
# `type` field; a `params object[]` tail is an unwrapped `array_type` child
# of the parameter_list (grammar quirk), captured directly.
TS_CSHARP_PARAMETER = "parameter"
TS_CSHARP_ARRAY_TYPE = "array_type"
TS_CSHARP_PARAMETER_LIST = "parameter_list"

# Local/field declarations for type inference. A local is a
# variable_declaration (type field + variable_declarator[s]); `var` makes the
# type field an implicit_type, so the type is inferred from the initializer.
# A field_declaration wraps a variable_declaration; a property_declaration
# exposes `type` and `name` fields directly.
TS_CSHARP_VARIABLE_DECLARATION = "variable_declaration"
TS_CSHARP_VARIABLE_DECLARATOR = "variable_declarator"
TS_CSHARP_IMPLICIT_TYPE = "implicit_type"
TS_CSHARP_FIELD_DECLARATION = "field_declaration"

# Call forms. A member call `recv.Method(...)` is an invocation_expression
# whose `function` field is a member_access_expression (`expression` receiver
# + `name` method); `this` is its own node type; args are `argument` nodes.
TS_CSHARP_INVOCATION_EXPRESSION = "invocation_expression"
TS_CSHARP_OBJECT_CREATION_EXPRESSION = "object_creation_expression"
# C# 9 target-typed `new()`: a distinct node with NO `type` field; the
# constructed type comes from the enclosing declaration (issue #773).
TS_CSHARP_IMPLICIT_OBJECT_CREATION_EXPRESSION = "implicit_object_creation_expression"
TS_CSHARP_MEMBER_ACCESS_EXPRESSION = "member_access_expression"
# A conditional call `recv?.Method(...)`: the invocation's `function` field
# is a conditional_access_expression whose member_binding_expression child
# carries the method name.
TS_CSHARP_CONDITIONAL_ACCESS_EXPRESSION = "conditional_access_expression"
TS_CSHARP_MEMBER_BINDING_EXPRESSION = "member_binding_expression"
TS_CSHARP_FIELD_EXPRESSION = "expression"
TS_CSHARP_THIS = "this"
TS_CSHARP_ARGUMENT = "argument"
# implicit_object_creation_expression (`new(...)`) exposes NO field names;
# its argument_list is an unfielded named child, so argument parsing needs
# a typed fallback where the `arguments` field lookup returns nothing.
TS_CSHARP_ARGUMENT_LIST = "argument_list"

# String literals: `"x"` is a string_literal wrapping a string_literal_content
# token. Used by the I/O walk to read a sink's static path/key argument.
TS_CSHARP_STRING_LITERAL = "string_literal"
TS_CSHARP_STRING_LITERAL_CONTENT = "string_literal_content"
# `map[k]` subscript; inert in the I/O walk (C# env access is a call, not a
# member read) but wired into the descriptor for shape correctness.
TS_CSHARP_ELEMENT_ACCESS_EXPRESSION = "element_access_expression"

# Nested scopes that own their own locals; the variable-type walk stops at
# these so a lambda/local-function local cannot leak into (or shadow) the
# enclosing method's type map.
TS_CSHARP_NESTED_SCOPE_TYPES = (
    "lambda_expression",
    "anonymous_method_expression",
    "local_function_statement",
)

# Import form: `using System;`, `using X = Y;`, `global using System.Linq;`.
TS_CSHARP_USING_DIRECTIVE = "using_directive"

# The name node inside a using directive: a dotted `qualified_name` or a bare
# `identifier` (both the imported path and, in the alias form, the alias).
TS_CSHARP_QUALIFIED_NAME = "qualified_name"
TS_CSHARP_IDENTIFIER = "identifier"

# Expression body `=> expr` on methods, properties, and accessors.
TS_CSHARP_ARROW_EXPRESSION_CLAUSE = "arrow_expression_clause"
# `public T this[int i] { ... }`: return-typed via `type` like a property.
TS_CSHARP_INDEXER_DECLARATION = "indexer_declaration"
# Some grammar versions wrap a declarator's `= value` initializer in an
# equals_value_clause node (the pinned grammar hangs the value directly off
# the declarator); the target-typed-new walk skips it either way, mirroring
# the initializer search in csharp/type_inference.py.
TS_CSHARP_EQUALS_VALUE_CLAUSE = "equals_value_clause"

# Field names used with child_by_field_name.
TS_CSHARP_FIELD_NAME = "name"
TS_CSHARP_FIELD_OPERATOR = "operator"
TS_CSHARP_FIELD_TYPE = "type"
# method_declaration/local_function_statement expose the return type via
# `returns` (there is no `type` field on them).
TS_CSHARP_FIELD_RETURNS = "returns"

# Operator/conversion-operator declarations expose no `name` field; a stable
# synthetic name is built from these prefixes so the node still gets a qn.
TS_CSHARP_OPERATOR_NAME_PREFIX = "operator_"
TS_CSHARP_DESTRUCTOR_NAME_PREFIX = "~"

# C# reserved keywords can never be identifiers, so a member/local-function
# whose `name` field is one is a parse-recovery artifact -- e.g. a `#if`
# directive splitting an if/else chain mid-method makes tree-sitter recover
# the trailing `else if (...)` as a local_function_statement named `if`. Drop
# those instead of emitting bogus Function nodes. Contextual keywords (`record`,
# `async`, `var`, ...) ARE valid identifiers, so only the reserved set is here.
CSHARP_RESERVED_KEYWORDS = frozenset(
    {
        "abstract",
        "as",
        "base",
        "bool",
        "break",
        "byte",
        "case",
        "catch",
        "char",
        "checked",
        "class",
        "const",
        "continue",
        "decimal",
        "default",
        "delegate",
        "do",
        "double",
        "else",
        "enum",
        "event",
        "explicit",
        "extern",
        "false",
        "finally",
        "fixed",
        "float",
        "for",
        "foreach",
        "goto",
        "if",
        "implicit",
        "in",
        "int",
        "interface",
        "internal",
        "is",
        "lock",
        "long",
        "namespace",
        "new",
        "null",
        "object",
        "operator",
        "out",
        "override",
        "params",
        "private",
        "protected",
        "public",
        "readonly",
        "ref",
        "return",
        "sbyte",
        "sealed",
        "short",
        "sizeof",
        "stackalloc",
        "static",
        "string",
        "struct",
        "switch",
        "this",
        "throw",
        "true",
        "try",
        "typeof",
        "uint",
        "ulong",
        "unchecked",
        "unsafe",
        "ushort",
        "using",
        "virtual",
        "void",
        "volatile",
        "while",
    }
)
