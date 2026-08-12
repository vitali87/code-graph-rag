# PHP tree-sitter node types.

TS_PHP_FUNCTION_DEFINITION = "function_definition"
TS_PHP_METHOD_DECLARATION = "method_declaration"
TS_PHP_TRAIT_DECLARATION = "trait_declaration"
# PHP inheritance clauses: `extends ...` (base_clause, for class AND
# interface) and `implements ...` (class_interface_clause); each lists `name`
# nodes for the base types.
TS_PHP_BASE_CLAUSE = "base_clause"
TS_PHP_CLASS_INTERFACE_CLAUSE = "class_interface_clause"
TS_PHP_NAME = "name"
# PHP fully-qualified base (`\Exception`, `\App\Base`); its trailing `name`
# child is the simple name cgr resolves against.
TS_PHP_QUALIFIED_NAME = "qualified_name"
TS_PHP_FUNCTION_STATIC_DECLARATION = "function_static_declaration"
TS_PHP_ANONYMOUS_FUNCTION = "anonymous_function"
TS_PHP_ARROW_FUNCTION = "arrow_function"
TS_PHP_MEMBER_CALL_EXPRESSION = "member_call_expression"
TS_PHP_SCOPED_CALL_EXPRESSION = "scoped_call_expression"
TS_PHP_FUNCTION_CALL_EXPRESSION = "function_call_expression"
TS_PHP_NULLSAFE_MEMBER_CALL_EXPRESSION = "nullsafe_member_call_expression"
TS_PHP_OBJECT_CREATION_EXPRESSION = "object_creation_expression"
TS_PHP_NAMESPACE_DEFINITION = "namespace_definition"
TS_PHP_NAMESPACE_USE_DECLARATION = "namespace_use_declaration"
TS_PHP_NAMESPACE_USE_CLAUSE = "namespace_use_clause"
TS_PHP_FUNCTION = "function"
TS_PHP_INCLUDE_EXPRESSION = "include_expression"
TS_PHP_INCLUDE_ONCE_EXPRESSION = "include_once_expression"
TS_PHP_REQUIRE_EXPRESSION = "require_expression"
TS_PHP_REQUIRE_ONCE_EXPRESSION = "require_once_expression"
TS_PHP_ATTRIBUTE_LIST = "attribute_list"
TS_PHP_ATTRIBUTE = "attribute"
TS_PHP_ATTRIBUTE_GROUP = "attribute_group"
TS_PHP_VISIBILITY_MODIFIER = "visibility_modifier"
TS_PHP_USE_DECLARATION = "use_declaration"

# FLOWS_TO lean-walk node types (issue #1174). PHP variables carry a `$` prefix
# (`variable_name` -> `.text == "$s"`), so a local can never shadow a builtin sink.
TS_PHP_VARIABLE_NAME = "variable_name"
TS_PHP_ENCAPSED_STRING = "encapsed_string"
TS_PHP_STRING_CONTENT = "string_content"
TS_PHP_COMPOUND_STATEMENT = "compound_statement"
TS_PHP_FORMAL_PARAMETERS = "formal_parameters"
TS_PHP_SIMPLE_PARAMETER = "simple_parameter"
TS_PHP_ARGUMENT = "argument"
TS_PHP_MEMBER_ACCESS_EXPRESSION = "member_access_expression"
TS_PHP_SUBSCRIPT_EXPRESSION = "subscript_expression"
# `echo $a, $b;` (echo_statement) and `print $x` (print_intrinsic) write STDOUT;
# multiple echo operands wrap in a sequence_expression.
TS_PHP_ECHO_STATEMENT = "echo_statement"
TS_PHP_PRINT_INTRINSIC = "print_intrinsic"
TS_PHP_SEQUENCE_EXPRESSION = "sequence_expression"
TS_PHP_FOREACH_STATEMENT = "foreach_statement"
TS_PHP_DEFAULT_STATEMENT = "default_statement"
# `if` emits its elseif/else chain as flat sibling `alternative` children; an
# `else_if_clause` is a CONDITIONAL alternative (so a chain ending in one still
# leaves an implicit skip path), an `else_clause` is the terminal not-then path.
TS_PHP_ELSE_IF_CLAUSE = "else_if_clause"
TS_PHP_ELSE_CLAUSE = "else_clause"
