# Go tree-sitter node types.

TS_GO_PACKAGE_CLAUSE = "package_clause"
TS_GO_PACKAGE_IDENTIFIER = "package_identifier"
TS_GO_TYPE_DECLARATION = "type_declaration"
TS_GO_TYPE_SPEC = "type_spec"
TS_GO_TYPE_ALIAS = "type_alias"
TS_GO_STRUCT_TYPE = "struct_type"
TS_GO_SELECTOR_EXPRESSION = "selector_expression"
TS_GO_TYPE_ASSERTION_EXPRESSION = "type_assertion_expression"
# A Go source file ending in `_test.go` is compiled only under `go test`; its
# module-qn file segment ends with this suffix. It may declare an EXTERNAL test
# package (`package p_test`) that shares a directory with `package p` but is
# distinct, so it must be excluded from same-directory fan-out.
GO_TEST_FILE_SUFFIX = "_test"
TS_GO_FIELD_DECLARATION_LIST = "field_declaration_list"
TS_GO_FIELD_DECLARATION = "field_declaration"
TS_GO_FIELD_IDENTIFIER = "field_identifier"
TS_GO_INTERFACE_TYPE = "interface_type"
TS_GO_PARAMETER_DECLARATION = "parameter_declaration"
TS_GO_FUNC_LITERAL = "func_literal"
TS_GO_SOURCE_FILE = "source_file"
TS_GO_FUNCTION_DECLARATION = "function_declaration"
TS_GO_METHOD_DECLARATION = "method_declaration"
TS_GO_CALL_EXPRESSION = "call_expression"
TS_GO_IMPORT_DECLARATION = "import_declaration"
TS_GO_PARAMETER_LIST = "parameter_list"
TS_GO_VAR_DECLARATION = "var_declaration"
TS_GO_VAR_SPEC = "var_spec"
TS_GO_SHORT_VAR_DECLARATION = "short_var_declaration"
TS_GO_ASSIGNMENT_STATEMENT = "assignment_statement"
# I/O detection (issue #714): a function body is a `block`; string arguments are
# `interpreted_string_literal` (double-quoted) whose text lives in a
# `interpreted_string_literal_content` child; `index_expression`/operand+field are
# the selector/subscript node shapes (only used by member-access reads, which Go
# has none of, so they are inert placeholders here).
TS_GO_IDENTIFIER = "identifier"
TS_GO_TYPE_IDENTIFIER = "type_identifier"
TS_GO_CONST_SPEC = "const_spec"
TS_GO_RANGE_CLAUSE = "range_clause"
# The init;cond;post header of a C-style Go for; its post statement lives in
# an `update` field.
TS_GO_FOR_CLAUSE = "for_clause"
# Switch family: arms are EXCLUSIVE (Go has no implicit fallthrough).
TS_GO_EXPRESSION_SWITCH_STATEMENT = "expression_switch_statement"
TS_GO_TYPE_SWITCH_STATEMENT = "type_switch_statement"
TS_GO_SELECT_STATEMENT = "select_statement"
TS_GO_EXPRESSION_CASE = "expression_case"
TS_GO_TYPE_CASE = "type_case"
TS_GO_COMMUNICATION_CASE = "communication_case"
TS_GO_DEFAULT_CASE = "default_case"
# Legal only as an arm's LAST statement; transfers into the next case.
TS_GO_FALLTHROUGH_STATEMENT = "fallthrough_statement"
TS_GO_STATEMENT_LIST = "statement_list"
TS_GO_BLOCK = "block"
# Go wraps a block's statements in a single `statement_list` node (unlike JS/Java);
# the source-order I/O walk unwraps it so per-statement shadowing sees the real
# statement boundaries.
TS_GO_STATEMENT_LIST = "statement_list"
TS_GO_INTERPRETED_STRING = "interpreted_string_literal"
TS_GO_INTERPRETED_STRING_CONTENT = "interpreted_string_literal_content"
TS_GO_RAW_STRING = "raw_string_literal"
TS_GO_RAW_STRING_CONTENT = "raw_string_literal_content"
TS_GO_DOT = "dot"
TS_GO_INDEX_EXPRESSION = "index_expression"
TS_GO_FIELD_OPERAND = "operand"
TS_GO_FIELD_FIELD = "field"
TS_GO_FIELD_INDEX = "index"
TS_GO_EXPRESSION_LIST = "expression_list"
TS_GO_COMPOSITE_LITERAL = "composite_literal"
TS_GO_LITERAL_VALUE = "literal_value"
TS_GO_KEYED_ELEMENT = "keyed_element"
TS_GO_LITERAL_ELEMENT = "literal_element"
TS_GO_UNARY_EXPRESSION = "unary_expression"
# `[]byte(s)` / `string(b)`: value-preserving conversion, unwrapped by the
# lean flow walk so the operand's taint carries through (issue #714).
TS_GO_TYPE_CONVERSION_EXPRESSION = "type_conversion_expression"
TS_GO_POINTER_TYPE = "pointer_type"
# `pkg.Type` in a signature; kept as dotted text so a binding typed to an
# external package's type stays TYPED (and drops) instead of trie-guessed.
TS_GO_QUALIFIED_TYPE = "qualified_type"
# Go composite types a method may return; a chained call lands on the CONTAINER,
# not its element, so return-type inference must not unwrap these (a `[]Command`
# return must not resolve `.Run()` to `Command.Run`).
TS_GO_CONTAINER_TYPES: frozenset[str] = frozenset(
    {"slice_type", "array_type", "map_type", "channel_type", "function_type"}
)
FIELD_OPERAND = "operand"
