# Tree-sitter-julia node types (tree-sitter-julia 0.23.1).

TS_JULIA_SOURCE_FILE = "source_file"
TS_JULIA_MODULE_DEFINITION = "module_definition"

# Type declarations. `struct_definition` covers both `struct` and
# `mutable struct`; the name lives in a positional `type_head` child (no
# `name` field), whose first named child is the name (a `binary_expression`
# when a `<:` base is present).
TS_JULIA_STRUCT_DEFINITION = "struct_definition"
TS_JULIA_ABSTRACT_DEFINITION = "abstract_definition"
TS_JULIA_PRIMITIVE_DEFINITION = "primitive_definition"
TS_JULIA_TYPE_HEAD = "type_head"
TS_JULIA_PARAMETRIZED_TYPE_EXPRESSION = "parametrized_type_expression"

# Functions. A `function_definition` holds a positional `signature` child
# wrapping the call-shaped head (name = the head's first named child) plus
# the body statements; the concise form `f(x) = body` is an `assignment`
# whose LEFT side is that call head (a plain `x = 1` assignment is NOT a
# function). `where`/return-type spellings wrap the head in
# `where_expression`/`typed_expression`.
TS_JULIA_FUNCTION_DEFINITION = "function_definition"
TS_JULIA_MACRO_DEFINITION = "macro_definition"
TS_JULIA_SIGNATURE = "signature"
TS_JULIA_ARROW_FUNCTION_EXPRESSION = "arrow_function_expression"
TS_JULIA_ASSIGNMENT = "assignment"
TS_JULIA_WHERE_EXPRESSION = "where_expression"
TS_JULIA_TYPED_EXPRESSION = "typed_expression"
# Head decoration: a concise method's return type and a `function` head's
# return type both wrap the call head in `typed_expression`, and a closure
# head `(x::T)(args)` is a `parenthesized_expression` around a typed/unary
# typed expression. Macro-emitted heads interpolate names (`$op(...)`),
# whose callee is an `interpolation_expression`.
TS_JULIA_PARENTHESIZED_EXPRESSION = "parenthesized_expression"
TS_JULIA_UNARY_TYPED_EXPRESSION = "unary_typed_expression"
TS_JULIA_INTERPOLATION_EXPRESSION = "interpolation_expression"

# Call sites. `call_expression` has NO `function` field: the callee is the
# first named child. `macrocall_expression` (`@name args`) is the macro
# namespace's invocation form; `broadcast_call_expression` is `f.(args)`.
TS_JULIA_CALL_EXPRESSION = "call_expression"
TS_JULIA_MACROCALL_EXPRESSION = "macrocall_expression"
TS_JULIA_MACRO_IDENTIFIER = "macro_identifier"
TS_JULIA_BROADCAST_CALL_EXPRESSION = "broadcast_call_expression"
TS_JULIA_FIELD_EXPRESSION = "field_expression"
TS_JULIA_SCOPED_IDENTIFIER = "scoped_identifier"
TS_JULIA_IDENTIFIER = "identifier"

# Imports: `using A.B`, `import A: b as c`. Relative `.`/`..` prefixes are
# dropped by the grammar, and `using A.B.*` parse-recoveries keep the
# statement node usable.
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
