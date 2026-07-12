# (H) Go tree-sitter node types.

# (H) Tree-sitter Go node types
TS_GO_TYPE_DECLARATION = "type_declaration"
TS_GO_TYPE_SPEC = "type_spec"
TS_GO_TYPE_ALIAS = "type_alias"
TS_GO_STRUCT_TYPE = "struct_type"
TS_GO_SELECTOR_EXPRESSION = "selector_expression"
TS_GO_TYPE_ASSERTION_EXPRESSION = "type_assertion_expression"
# (H) A Go source file whose name ends in `_test.go` is compiled only under `go test`;
# (H) its file segment in a module qn ends with this suffix. Such a file may declare an
# (H) EXTERNAL test package (`package p_test`) that shares a directory with `package p`
# (H) but is a distinct package, so it must be excluded from same-directory fan-out.
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
TS_GO_EXPRESSION_LIST = "expression_list"
TS_GO_COMPOSITE_LITERAL = "composite_literal"
TS_GO_LITERAL_VALUE = "literal_value"
TS_GO_KEYED_ELEMENT = "keyed_element"
TS_GO_LITERAL_ELEMENT = "literal_element"
TS_GO_UNARY_EXPRESSION = "unary_expression"
TS_GO_POINTER_TYPE = "pointer_type"
# (H) Go composite types a method may return; a chained call lands on the CONTAINER,
# (H) not its element, so return-type inference must not unwrap these to an element
# (H) name (a `[]Command` return must not resolve `.Run()` to `Command.Run`).
TS_GO_CONTAINER_TYPES: frozenset[str] = frozenset(
    {"slice_type", "array_type", "map_type", "channel_type", "function_type"}
)
FIELD_OPERAND = "operand"
