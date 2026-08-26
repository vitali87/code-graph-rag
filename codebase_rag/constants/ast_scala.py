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
TS_SCALA_STRING = "string"
TS_SCALA_BLOCK = "block"
TS_SCALA_TEMPLATE_BODY = "template_body"
TS_SCALA_VAL_DEFINITION = "val_definition"
TS_SCALA_VAR_DEFINITION = "var_definition"
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
