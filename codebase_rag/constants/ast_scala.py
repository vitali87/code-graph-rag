# Scala tree-sitter node types.

TS_SCALA_CLASS_DEFINITION = "class_definition"
TS_SCALA_OBJECT_DEFINITION = "object_definition"
TS_SCALA_TRAIT_DEFINITION = "trait_definition"
TS_SCALA_COMPILATION_UNIT = "compilation_unit"
TS_SCALA_FUNCTION_DEFINITION = "function_definition"
TS_SCALA_FUNCTION_DECLARATION = "function_declaration"
TS_SCALA_CALL_EXPRESSION = "call_expression"
# Shared tree-sitter node type: a call with explicit type args, e.g. Rust
# turbofish `f::<T>()` and Scala `f[T]()`. Its `function` field holds the
# callee (identifier or scoped_identifier).
TS_GENERIC_FUNCTION = "generic_function"
TS_SCALA_GENERIC_FUNCTION = TS_GENERIC_FUNCTION
TS_SCALA_FIELD_EXPRESSION = "field_expression"
TS_SCALA_INFIX_EXPRESSION = "infix_expression"
TS_SCALA_IMPORT_DECLARATION = "import_declaration"
# `import a.b.{C, D}` / `import a.b.{C => Alias}` / `import a.b._`
TS_SCALA_NAMESPACE_SELECTORS = "namespace_selectors"
TS_SCALA_ARROW_RENAMED_IDENTIFIER = "arrow_renamed_identifier"
TS_SCALA_NAMESPACE_WILDCARD = "namespace_wildcard"
# Scala 3 spells a rename `as`; Scala 2 spells it `=>`. Different node types.
TS_SCALA_AS_RENAMED_IDENTIFIER = "as_renamed_identifier"
TS_SCALA_IMPORT_KEYWORD = "import"
# Import-map key prefix of a wildcard (`import a.b._` -> `*a.b`): it binds no
# name, so no identifier can collide with it.
SCALA_WILDCARD_PREFIX = "*"

# Wrappers the grammar puts between an `extends` clause and the
# `type_identifier` naming the base. `type_arguments` is deliberately ABSENT:
# it also holds a type_identifier (the `Int` of `Service[Int]`), so descending
# into it would return the type ARGUMENT as the base class.
TS_SCALA_GENERIC_TYPE = "generic_type"
TS_SCALA_STABLE_TYPE_IDENTIFIER = "stable_type_identifier"
TS_SCALA_STRING = "string"
TS_SCALA_BLOCK = "block"
TS_SCALA_TEMPLATE_BODY = "template_body"
TS_SCALA_VAL_DEFINITION = "val_definition"
TS_SCALA_VAR_DEFINITION = "var_definition"
TS_SCALA_ACCESS_MODIFIER = "access_modifier"
TS_SCALA_LAMBDA_EXPRESSION = "lambda_expression"
TS_SCALA_IDENTIFIER = "identifier"
TS_SCALA_INSTANCE_EXPRESSION = "instance_expression"
TS_SCALA_TYPE_IDENTIFIER = "type_identifier"

# Scala parameter shapes for lean slot extraction (issue #1365). A repeated
# parameter (`xs: String*`) is spelled as the parameter's TYPE node.
TS_SCALA_PARAMETER = "parameter"
TS_SCALA_REPEATED_PARAMETER_TYPE = "repeated_parameter_type"
TS_SCALA_INDENTED_BLOCK = "indented_block"
# A def declared `Unit` discards its body's value, so no return summary.
SCALA_UNIT_TYPE = "Unit"
# Every spelling of scala.Unit. A user-defined type merely ENDING in Unit
# (`example.Unit`) is a real type whose return value must still compose.
SCALA_UNIT_TYPES = frozenset({"Unit", "scala.Unit", "_root_.scala.Unit"})

# A curried `def f(a: Int)(b: Int)` has one `parameters` child per list.
TS_SCALA_PARAMETERS = "parameters"
TS_SCALA_FIELD_DEFAULT_VALUE = "default_value"

# Package clauses (issue #2450). `package a.b` heads a file and chained
# clauses nest (`package a` then `package b` is `a.b`); a clause with a
# body, `package a { ... }`, scopes only that body. `package object p`
# declares members of package `p` itself.
TS_SCALA_PACKAGE_CLAUSE = "package_clause"
TS_SCALA_PACKAGE_IDENTIFIER = "package_identifier"
TS_SCALA_PACKAGE_OBJECT = "package_object"
# `_root_.a.b` anchors an import at the root, past every enclosing package.
SCALA_ROOT_PACKAGE = "_root_"
TS_SCALA_ENUM_DEFINITION = "enum_definition"
TS_SCALA_TYPE_DEFINITION = "type_definition"
TS_SCALA_GIVEN_DEFINITION = "given_definition"
# Definitions that put a NAME into the package they sit in. A val/var names
# its binding through `pattern` instead, so it is listed apart.
SCALA_NAMED_PACKAGE_MEMBERS = frozenset(
    {
        TS_SCALA_CLASS_DEFINITION,
        TS_SCALA_OBJECT_DEFINITION,
        TS_SCALA_TRAIT_DEFINITION,
        TS_SCALA_FUNCTION_DEFINITION,
        TS_SCALA_FUNCTION_DECLARATION,
        TS_SCALA_ENUM_DEFINITION,
        TS_SCALA_TYPE_DEFINITION,
        TS_SCALA_GIVEN_DEFINITION,
    }
)
SCALA_BINDING_DEFINITIONS = frozenset(
    {TS_SCALA_VAL_DEFINITION, TS_SCALA_VAR_DEFINITION}
)

# `new C(...)` and the receiver shapes a parameterless selection is typed by.
TS_SCALA_COMPOUND_TYPE = "compound_type"
TS_SCALA_FIELD_BASE = "base"
TS_SCALA_LAZY_PARAMETER_TYPE = "lazy_parameter_type"
TS_SCALA_ASSIGNMENT_EXPRESSION = "assignment_expression"
SCALA_THIS = "this"
# An auxiliary constructor is a `def this(...)`, registered as a method named
# `this` on its class; `new C(...)` may run any of them.
SCALA_AUXILIARY_CONSTRUCTOR = SCALA_THIS
