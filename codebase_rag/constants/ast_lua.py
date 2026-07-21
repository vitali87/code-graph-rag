# Lua tree-sitter node types and string forms.

from .ast_nodes import TS_STRING, TS_STRING_LITERAL

LUA_STRING_TYPES = (TS_STRING, TS_STRING_LITERAL)

# Tree-sitter Lua node types
TS_DOT_INDEX_EXPRESSION = "dot_index_expression"
TS_LUA_VARIABLE_DECLARATION = "variable_declaration"
TS_LUA_ASSIGNMENT_STATEMENT = "assignment_statement"
TS_LUA_VARIABLE_LIST = "variable_list"
TS_LUA_EXPRESSION_LIST = "expression_list"
TS_LUA_FUNCTION_CALL = "function_call"
TS_LUA_METHOD_INDEX_EXPRESSION = "method_index_expression"
TS_LUA_IDENTIFIER = "identifier"
TS_LUA_LOCAL_STATEMENT = "local_statement"
LUA_STATEMENT_SUFFIX = "statement"
LUA_DEFAULT_VAR_TYPES = (TS_LUA_IDENTIFIER,)

# Lua method separator
LUA_METHOD_SEPARATOR = ":"

# Tree-sitter Lua node types for language_spec
TS_LUA_CHUNK = "chunk"
TS_LUA_FUNCTION_DECLARATION = "function_declaration"
TS_LUA_FUNCTION_DEFINITION = "function_definition"

# Import processor function names
IMPORT_REQUIRE = "require"
IMPORT_PCALL = "pcall"
IMPORT_IMPORT = "import"
