# Tree-sitter-julia node types (tree-sitter-julia 0.23.1).

TS_JULIA_SOURCE_FILE = "source_file"
TS_JULIA_MODULE_DEFINITION = "module_definition"

# Type declarations. The name is in a positional `type_head` child (no
# `name` field); a `<:` base wraps it in a `binary_expression`.
TS_JULIA_STRUCT_DEFINITION = "struct_definition"
TS_JULIA_ABSTRACT_DEFINITION = "abstract_definition"
TS_JULIA_PRIMITIVE_DEFINITION = "primitive_definition"
TS_JULIA_TYPE_HEAD = "type_head"
TS_JULIA_PARAMETRIZED_TYPE_EXPRESSION = "parametrized_type_expression"

# Functions. The head is a positional `signature` child; the concise form
# `f(x) = body` is an `assignment` whose LEFT side is the head. Return
# types / `where` clauses wrap the head.
TS_JULIA_FUNCTION_DEFINITION = "function_definition"
# No body field in the grammar: the body span is derived between the
# signature and this `end` token.
TS_JULIA_END_KEYWORD = "end"
TS_JULIA_MACRO_DEFINITION = "macro_definition"
TS_JULIA_SIGNATURE = "signature"
TS_JULIA_ARROW_FUNCTION_EXPRESSION = "arrow_function_expression"
TS_JULIA_ASSIGNMENT = "assignment"
TS_JULIA_WHERE_EXPRESSION = "where_expression"
TS_JULIA_TYPED_EXPRESSION = "typed_expression"
# Head wrappers: return types use `typed_expression`, closure heads
# `(x::T)(args)` a `parenthesized_expression`, macro-emitted names
# (`$op(...)`) an `interpolation_expression`.
TS_JULIA_PARENTHESIZED_EXPRESSION = "parenthesized_expression"
TS_JULIA_UNARY_TYPED_EXPRESSION = "unary_typed_expression"
TS_JULIA_INTERPOLATION_EXPRESSION = "interpolation_expression"
TS_JULIA_ARGUMENT_LIST = "argument_list"

# Call sites. `call_expression` has no `function` field (callee = first
# named child); `macrocall_expression` is `@name args`.
TS_JULIA_CALL_EXPRESSION = "call_expression"
TS_JULIA_MACROCALL_EXPRESSION = "macrocall_expression"
TS_JULIA_MACRO_IDENTIFIER = "macro_identifier"
TS_JULIA_BROADCAST_CALL_EXPRESSION = "broadcast_call_expression"
TS_JULIA_FIELD_EXPRESSION = "field_expression"
TS_JULIA_SCOPED_IDENTIFIER = "scoped_identifier"
TS_JULIA_IDENTIFIER = "identifier"

# Imports. Relative `.`/`..` prefixes are dropped by the grammar.
TS_JULIA_USING_STATEMENT = "using_statement"
TS_JULIA_IMPORT_STATEMENT = "import_statement"
TS_JULIA_SELECTED_IMPORT = "selected_import"
TS_JULIA_IMPORT_PATH = "import_path"
TS_JULIA_IMPORT_ALIAS = "import_alias"

TS_JULIA_CONST_STATEMENT = "const_statement"
TS_JULIA_GLOBAL_STATEMENT = "global_statement"
TS_JULIA_LOCAL_STATEMENT = "local_statement"
TS_JULIA_LINE_COMMENT = "line_comment"
TS_JULIA_BLOCK_COMMENT = "block_comment"

# Julia type declaration node types (for the class pass).
JULIA_TYPE_DEFINITION_TYPES = (
    TS_JULIA_STRUCT_DEFINITION,
    TS_JULIA_ABSTRACT_DEFINITION,
    TS_JULIA_PRIMITIVE_DEFINITION,
)
