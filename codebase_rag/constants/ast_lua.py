# Lua tree-sitter node types and string forms.

from .ast_nodes import TS_STRING, TS_STRING_LITERAL

LUA_STRING_TYPES = (TS_STRING, TS_STRING_LITERAL)

TS_DOT_INDEX_EXPRESSION = "dot_index_expression"
TS_LUA_BRACKET_INDEX_EXPRESSION = "bracket_index_expression"
TS_LUA_VARIABLE_DECLARATION = "variable_declaration"
TS_LUA_ASSIGNMENT_STATEMENT = "assignment_statement"
TS_LUA_VARIABLE_LIST = "variable_list"
TS_LUA_EXPRESSION_LIST = "expression_list"
TS_LUA_FUNCTION_CALL = "function_call"
TS_LUA_METHOD_INDEX_EXPRESSION = "method_index_expression"
TS_LUA_IDENTIFIER = "identifier"
TS_LUA_STRING_CONTENT = "string_content"
TS_LUA_BLOCK = "block"
# `dot_index_expression` / `bracket_index_expression` field names: the accessed
# table is `table`, the member/key is `field`.
TS_LUA_FIELD_TABLE = "table"
TS_LUA_LOCAL_STATEMENT = "local_statement"
LUA_STATEMENT_SUFFIX = "statement"
LUA_DEFAULT_VAR_TYPES = (TS_LUA_IDENTIFIER,)

LUA_METHOD_SEPARATOR = ":"
LUA_FIELD_SEPARATOR = "."
# `setmetatable(obj, T)` returns `obj` with `T` as its metatable: the idiom
# that makes `obj` an instance of `T`.
LUA_SETMETATABLE = "setmetatable"

# Tree-sitter Lua node types for language_spec
TS_LUA_CHUNK = "chunk"
TS_LUA_FUNCTION_DECLARATION = "function_declaration"
TS_LUA_FUNCTION_DEFINITION = "function_definition"
# A `key = value` entry of a table constructor (`{ f = function() end }`): the
# key is the `name` field (an identifier, or a string for `["key"] =`), the
# value the `value` field.
TS_LUA_FIELD = "field"
# The token that opens a computed or string key: `["k"] = v`, `[k] = v`.
LUA_OPEN_BRACKET = "["
TS_LUA_TABLE_CONSTRUCTOR = "table_constructor"
# What can redeclare a name inside a chunk (Greptile, PR #2617): the `local`
# keyword that opens a `local function`, a `for` loop's variables (the
# `clause` field: a generic clause's `variable_list`, or a numeric clause's
# `name`), and a function's parameters.
TS_LUA_LOCAL_KEYWORD = "local"
TS_LUA_FOR_STATEMENT = "for_statement"
TS_LUA_FIELD_CLAUSE = "clause"

# Import processor function names
IMPORT_REQUIRE = "require"
IMPORT_PCALL = "pcall"
IMPORT_IMPORT = "import"

# Lua `...` in a parameter list: occupies the trailing variadic slot and
# binds no simple name (issue #1365).
TS_LUA_VARARG_EXPRESSION = "vararg_expression"

# Assignment targets that name the function assigned to them: `f = function`,
# `T.f = function`, and `T["f"] = function`, which is the same binding as
# `T.f` when the key is a Lua name (issue #2578).
LUA_NAMING_ASSIGNMENT_TARGETS = (
    TS_DOT_INDEX_EXPRESSION,
    TS_LUA_IDENTIFIER,
    TS_LUA_BRACKET_INDEX_EXPRESSION,
)
# Words a Lua name cannot be (Lua 5.4 manual, section 3.1): a string key
# spelling one (`t["end"]`) has no dotted form to mirror.
LUA_RESERVED_WORDS = frozenset(
    {
        "and",
        "break",
        "do",
        "else",
        "elseif",
        "end",
        "false",
        "for",
        "function",
        "goto",
        "if",
        "in",
        "local",
        "nil",
        "not",
        "or",
        "repeat",
        "return",
        "then",
        "true",
        "until",
        "while",
    }
)
