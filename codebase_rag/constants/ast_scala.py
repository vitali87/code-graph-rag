# (H) Scala tree-sitter node types.

# (H) Tree-sitter Scala node types
TS_SCALA_CLASS_DEFINITION = "class_definition"
TS_SCALA_OBJECT_DEFINITION = "object_definition"
TS_SCALA_TRAIT_DEFINITION = "trait_definition"
TS_SCALA_COMPILATION_UNIT = "compilation_unit"
TS_SCALA_FUNCTION_DEFINITION = "function_definition"
TS_SCALA_FUNCTION_DECLARATION = "function_declaration"
TS_SCALA_CALL_EXPRESSION = "call_expression"
# (H) Shared tree-sitter node type: a call with explicit type args, e.g. Rust
# (H) turbofish `f::<T>()` and Scala `f[T]()`. Its `function` field holds the
# (H) actual callee (identifier or scoped_identifier).
TS_GENERIC_FUNCTION = "generic_function"
TS_SCALA_GENERIC_FUNCTION = TS_GENERIC_FUNCTION
TS_SCALA_FIELD_EXPRESSION = "field_expression"
TS_SCALA_INFIX_EXPRESSION = "infix_expression"
TS_SCALA_IMPORT_DECLARATION = "import_declaration"
