//! The Go grammar's node kinds and field names the Go engine reads, as
//! constants (docs/reports/PHASE3_GO.md, section 3). A test asks the
//! compiled grammar for each (`Language::id_for_node_kind`,
//! `field_id_for_name`), so a name the grammar lacks fails the build's tests
//! instead of matching nothing; `codebase_rag/tests/test_engine_go.py`
//! checks that every one Go's Python analysis reads is here.

/// The constants of one table and the table of them, in one place, so no
/// constant can be left out of what the tests check.
macro_rules! names {
    ($(#[$doc:meta])* $table:ident { $($(#[$item:meta])* $name:ident = $text:literal,)* }) => {
        $($(#[$item])* pub const $name: &str = $text;)*
        $(#[$doc])*
        pub const $table: &[&str] = &[$($name),*];
    };
}

names! {
    /// Named node kinds.
    NODE_KINDS {
        ARRAY_TYPE = "array_type",
        ASSIGNMENT_STATEMENT = "assignment_statement",
        BLOCK = "block",
        CALL_EXPRESSION = "call_expression",
        CHANNEL_TYPE = "channel_type",
        COMMUNICATION_CASE = "communication_case",
        COMPOSITE_LITERAL = "composite_literal",
        CONST_DECLARATION = "const_declaration",
        CONST_SPEC = "const_spec",
        DEFAULT_CASE = "default_case",
        DOT = "dot",
        EXPRESSION_CASE = "expression_case",
        EXPRESSION_LIST = "expression_list",
        EXPRESSION_SWITCH_STATEMENT = "expression_switch_statement",
        FALLTHROUGH_STATEMENT = "fallthrough_statement",
        FIELD_DECLARATION = "field_declaration",
        FIELD_DECLARATION_LIST = "field_declaration_list",
        FIELD_IDENTIFIER = "field_identifier",
        FOR_CLAUSE = "for_clause",
        FOR_STATEMENT = "for_statement",
        FUNC_LITERAL = "func_literal",
        FUNCTION_DECLARATION = "function_declaration",
        FUNCTION_TYPE = "function_type",
        GENERIC_TYPE = "generic_type",
        IDENTIFIER = "identifier",
        IF_STATEMENT = "if_statement",
        IMPORT_DECLARATION = "import_declaration",
        INDEX_EXPRESSION = "index_expression",
        INTERFACE_TYPE = "interface_type",
        INTERPRETED_STRING_LITERAL = "interpreted_string_literal",
        INTERPRETED_STRING_LITERAL_CONTENT = "interpreted_string_literal_content",
        KEYED_ELEMENT = "keyed_element",
        LITERAL_ELEMENT = "literal_element",
        LITERAL_VALUE = "literal_value",
        MAP_TYPE = "map_type",
        METHOD_DECLARATION = "method_declaration",
        METHOD_ELEM = "method_elem",
        PACKAGE_CLAUSE = "package_clause",
        PACKAGE_IDENTIFIER = "package_identifier",
        PARAMETER_DECLARATION = "parameter_declaration",
        PARAMETER_LIST = "parameter_list",
        PARENTHESIZED_EXPRESSION = "parenthesized_expression",
        POINTER_TYPE = "pointer_type",
        QUALIFIED_TYPE = "qualified_type",
        RANGE_CLAUSE = "range_clause",
        RAW_STRING_LITERAL = "raw_string_literal",
        RAW_STRING_LITERAL_CONTENT = "raw_string_literal_content",
        RECEIVE_STATEMENT = "receive_statement",
        SELECT_STATEMENT = "select_statement",
        SELECTOR_EXPRESSION = "selector_expression",
        SHORT_VAR_DECLARATION = "short_var_declaration",
        SLICE_TYPE = "slice_type",
        SOURCE_FILE = "source_file",
        STATEMENT_LIST = "statement_list",
        STRUCT_TYPE = "struct_type",
        TYPE_ALIAS = "type_alias",
        TYPE_ARGUMENTS = "type_arguments",
        TYPE_ASSERTION_EXPRESSION = "type_assertion_expression",
        TYPE_CASE = "type_case",
        TYPE_CONVERSION_EXPRESSION = "type_conversion_expression",
        TYPE_DECLARATION = "type_declaration",
        TYPE_ELEM = "type_elem",
        TYPE_IDENTIFIER = "type_identifier",
        TYPE_PARAMETER_DECLARATION = "type_parameter_declaration",
        TYPE_SPEC = "type_spec",
        TYPE_SWITCH_STATEMENT = "type_switch_statement",
        UNARY_EXPRESSION = "unary_expression",
        VAR_DECLARATION = "var_declaration",
        VAR_SPEC = "var_spec",
        VAR_SPEC_LIST = "var_spec_list",
        VARIADIC_ARGUMENT = "variadic_argument",
        VARIADIC_PARAMETER_DECLARATION = "variadic_parameter_declaration",
    }
}

names! {
    /// Anonymous node kinds: the tokens read by their text.
    TOKENS {
        /// `for k, v := range xs` declares its variables; `=` assigns ones
        /// declared before.
        DECLARE_ASSIGN = ":=",
    }
}

names! {
    /// Field names.
    FIELDS {
        FIELD_ALIAS = "alias",
        FIELD_BODY = "body",
        FIELD_ELEMENT = "element",
        FIELD_FIELD = "field",
        FIELD_FUNCTION = "function",
        FIELD_INDEX = "index",
        FIELD_INITIALIZER = "initializer",
        FIELD_KEY = "key",
        FIELD_LEFT = "left",
        FIELD_NAME = "name",
        FIELD_OPERAND = "operand",
        FIELD_PACKAGE = "package",
        FIELD_PARAMETERS = "parameters",
        FIELD_RECEIVER = "receiver",
        FIELD_RESULT = "result",
        FIELD_RIGHT = "right",
        FIELD_TYPE = "type",
        FIELD_TYPE_ARGUMENTS = "type_arguments",
        FIELD_TYPE_PARAMETERS = "type_parameters",
        FIELD_VALUE = "value",
    }
}
