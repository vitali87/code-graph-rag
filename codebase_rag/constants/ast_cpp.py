# C/C++ tree-sitter node types, module markers, and operator maps.

from enum import StrEnum

from .ast_nodes import TS_ENUM_SPECIFIER, TS_STRUCT_SPECIFIER, TS_UNION_SPECIFIER


class CppNodeType(StrEnum):
    TRANSLATION_UNIT = "translation_unit"
    NAMESPACE_DEFINITION = "namespace_definition"
    NAMESPACE_IDENTIFIER = "namespace_identifier"
    IDENTIFIER = "identifier"
    EXPORT = "export"
    EXPORT_KEYWORD = "export_keyword"
    PRIMITIVE_TYPE = "primitive_type"
    DECLARATION = "declaration"
    FUNCTION_DEFINITION = "function_definition"
    TEMPLATE_DECLARATION = "template_declaration"
    CLASS_SPECIFIER = "class_specifier"
    FUNCTION_DECLARATOR = "function_declarator"
    POINTER_DECLARATOR = "pointer_declarator"
    REFERENCE_DECLARATOR = "reference_declarator"
    # An attribute MACRO before a definition (`JSON_HEDLEY_NON_NULL(3)
    # bool sax_parse(...)`) parses as a parenthesized_declarator wrapping
    # an ERROR plus the real function_declarator; the name walk descends it.
    PARENTHESIZED_DECLARATOR = "parenthesized_declarator"
    FIELD_DECLARATION = "field_declaration"
    FIELD_IDENTIFIER = "field_identifier"
    FIELD_INITIALIZER_LIST = "field_initializer_list"
    FIELD_INITIALIZER = "field_initializer"
    TEMPLATE_METHOD = "template_method"
    QUALIFIED_IDENTIFIER = "qualified_identifier"
    OPERATOR_NAME = "operator_name"
    DESTRUCTOR_NAME = "destructor_name"
    CONSTRUCTOR_OR_DESTRUCTOR_DEFINITION = "constructor_or_destructor_definition"
    CONSTRUCTOR_OR_DESTRUCTOR_DECLARATION = "constructor_or_destructor_declaration"
    INLINE_METHOD_DEFINITION = "inline_method_definition"
    OPERATOR_CAST_DEFINITION = "operator_cast_definition"
    TYPE_IDENTIFIER = "type_identifier"
    PARAMETER_LIST = "parameter_list"
    PARAMETER_DECLARATION = "parameter_declaration"
    OPTIONAL_PARAMETER_DECLARATION = "optional_parameter_declaration"
    INIT_DECLARATOR = "init_declarator"
    TEMPLATE_TYPE = "template_type"
    FIELD_EXPRESSION = "field_expression"
    COMPOUND_STATEMENT = "compound_statement"
    THIS = "this"
    TYPE_DEFINITION = "type_definition"
    ALIAS_DECLARATION = "alias_declaration"
    TYPE_DESCRIPTOR = "type_descriptor"


CPP_MODULE_EXTENSIONS = (".ixx", ".cppm", ".ccm", ".mxx")
CPP_MODULE_PATH_MARKERS = frozenset({"interfaces", "modules"})

# C++ module declaration prefixes
CPP_EXPORT_MODULE_PREFIX = "export module "
CPP_MODULE_PREFIX = "module "
CPP_MODULE_PRIVATE_PREFIX = "module ;"
CPP_IMPL_SUFFIX = "_impl"

# C++ module type values
CPP_MODULE_TYPE_INTERFACE = "interface"
CPP_MODULE_TYPE_IMPLEMENTATION = "implementation"

# C++ export prefixes for class detection
CPP_EXPORT_CLASS_PREFIX = "export class "
CPP_EXPORT_STRUCT_PREFIX = "export struct "
CPP_EXPORT_UNION_PREFIX = "export union "
CPP_EXPORT_TEMPLATE_PREFIX = "export template"
CPP_EXPORT_PREFIXES = (
    CPP_EXPORT_CLASS_PREFIX,
    CPP_EXPORT_STRUCT_PREFIX,
    CPP_EXPORT_UNION_PREFIX,
    CPP_EXPORT_TEMPLATE_PREFIX,
)

# C++ keywords for class detection
CPP_KEYWORD_CLASS = "class"
CPP_KEYWORD_STRUCT = "struct"
CPP_EXPORTED_CLASS_KEYWORDS = frozenset({CPP_KEYWORD_CLASS, CPP_KEYWORD_STRUCT})

# A C/C++ class/struct/union tag with no body is a forward declaration
# (`class Widget;`); making it its own node collides with the real
# definition's qn and fragments the class into same-named nodes.
CPP_TYPE_SPECIFIER_NODE_TYPES = frozenset(
    {"class_specifier", "struct_specifier", "union_specifier"}
)

CPP_FALLBACK_OPERATOR = "operator_unknown"
CPP_FALLBACK_DESTRUCTOR = "~destructor"
CPP_OPERATOR_TEXT_PREFIX = "operator"
CPP_DESTRUCTOR_PREFIX = "~"

CPP_OPERATOR_SYMBOL_MAP: dict[str, str] = {
    "+": "operator_plus",
    "-": "operator_minus",
    "*": "operator_multiply",
    "/": "operator_divide",
    "%": "operator_modulo",
    "=": "operator_assign",
    "==": "operator_equal",
    "!=": "operator_not_equal",
    "<": "operator_less",
    ">": "operator_greater",
    "<=": "operator_less_equal",
    ">=": "operator_greater_equal",
    "&&": "operator_logical_and",
    "||": "operator_logical_or",
    "&": "operator_bitwise_and",
    "|": "operator_bitwise_or",
    "^": "operator_bitwise_xor",
    "~": "operator_bitwise_not",
    "!": "operator_not",
    "<<": "operator_left_shift",
    ">>": "operator_right_shift",
    "++": "operator_increment",
    "--": "operator_decrement",
    "+=": "operator_plus_assign",
    "-=": "operator_minus_assign",
    "*=": "operator_multiply_assign",
    "/=": "operator_divide_assign",
    "%=": "operator_modulo_assign",
    "&=": "operator_and_assign",
    "|=": "operator_or_assign",
    "^=": "operator_xor_assign",
    "<<=": "operator_left_shift_assign",
    ">>=": "operator_right_shift_assign",
    "[]": "operator_subscript",
    "()": "operator_call",
}

# Tree-sitter C++ node types for language_spec
TS_CPP_FUNCTION_DEFINITION = "function_definition"
TS_CPP_DECLARATION = "declaration"
TS_CPP_FIELD_DECLARATION = "field_declaration"
TS_CPP_TEMPLATE_DECLARATION = "template_declaration"
TS_CPP_TEMPLATE_PARAMETER_LIST = "template_parameter_list"
# The template TYPE-parameter declaration node types. A value/non-type param
# (`parameter_declaration`, e.g. `int N` / `MyEnum E`) and a template-template param
# are deliberately excluded: their type name is a concrete type, not a stand-in that
# a call receiver could be instantiated as, so it must not enter the template-param set.
CPP_TYPE_PARAMETER_DECL_TYPES = frozenset(
    {
        "type_parameter_declaration",
        "optional_type_parameter_declaration",
        "variadic_type_parameter_declaration",
    }
)
TS_CPP_LAMBDA_EXPRESSION = "lambda_expression"
TS_CPP_TRANSLATION_UNIT = "translation_unit"
TS_CPP_LINKAGE_SPECIFICATION = "linkage_specification"
TS_CPP_CALL_EXPRESSION = "call_expression"
TS_CPP_FIELD_EXPRESSION = "field_expression"
TS_CPP_SUBSCRIPT_EXPRESSION = "subscript_expression"
TS_CPP_NEW_EXPRESSION = "new_expression"
TS_CPP_DELETE_EXPRESSION = "delete_expression"
TS_CPP_BINARY_EXPRESSION = "binary_expression"
TS_CPP_UNARY_EXPRESSION = "unary_expression"
TS_CPP_UPDATE_EXPRESSION = "update_expression"
TS_CPP_FUNCTION_DECLARATOR = "function_declarator"
# Substring shared by C++ declarator node types (pointer_declarator,
# reference_declarator, ...), used to unwrap a parameter declarator down
# to its bound identifier.
CPP_DECLARATOR_SUFFIX = "declarator"

FIELD_OPERATOR = "operator"
FIELD_MACRO = "macro"

# C++ I/O direct-sink walk node types (issue #714). call_expression keeps a
# `function` field so call_name works unchanged. The stdout write path is the
# stream-insertion operator `std::cout << x` -- a `binary_expression` with a `<<`
# operator whose left-spine base is cout/cerr (no call node), handled via
# stream_sink_type like Rust's macro sinks. A string_literal wraps string_content;
# `compound_statement` is the block scope; `declaration` holds init_declarator
# locals whose bound name is nested under the `declarator` field.
TS_CPP_STRING_LITERAL = "string_literal"
TS_CPP_STRING_CONTENT = "string_content"
TS_CPP_COMPOUND_STATEMENT = "compound_statement"
# `if (int x = 1)` / `switch (int q = f())`: the condition declaration nests
# inside this wrapper, one level below the statement node.
TS_CPP_CONDITION_CLAUSE = "condition_clause"
# A lambda's parameter list hangs off this declarator (no name to declare).
TS_CPP_ABSTRACT_FUNCTION_DECLARATOR = "abstract_function_declarator"
TS_CPP_DECLARATION = "declaration"
TS_CPP_INIT_DECLARATOR = "init_declarator"
TS_CPP_PARAMETER_DECLARATION = "parameter_declaration"
TS_CPP_IDENTIFIER = "identifier"
TS_CPP_QUALIFIED_IDENTIFIER = "qualified_identifier"
# `Reader<T>(...)` as a call target: the callee wraps name + template args.
TS_CPP_TEMPLATE_FUNCTION = "template_function"
# `return {args};` -- a braced construction of the declared return type.
TS_CPP_INITIALIZER_LIST = "initializer_list"
# Stream-insertion operator; a `binary_expression` using it whose left-spine base
# is std::cout / std::cerr writes STDOUT.
CPP_OP_LEFT_SHIFT = "<<"
# Stream-extraction operator; on a bound fstream handle (`in >> word`) it is a
# READ of that handle's resource (issue #714).
CPP_OP_RIGHT_SHIFT = ">>"
TS_CPP_FOR_RANGE_LOOP = "for_range_loop"
# Switch family: cases may fall through; a default arm is a
# case_statement without a `value` field.
TS_CPP_SWITCH_STATEMENT = "switch_statement"
TS_CPP_CASE_STATEMENT = "case_statement"
# field_expression = `obj.field` (argument/field); subscript_expression =
# `arr[i]` (argument/indices). Inert for C++ I/O, wired for shape correctness.
CPP_FIELD_ARGUMENT = "argument"
CPP_FIELD_FIELD = "field"
CPP_FIELD_INDICES = "indices"

# Derived node type tuples for class ingestion
CPP_CLASS_TYPES = (CppNodeType.CLASS_SPECIFIER, TS_STRUCT_SPECIFIER)
CPP_COMPOUND_TYPES = (*CPP_CLASS_TYPES, TS_UNION_SPECIFIER, TS_ENUM_SPECIFIER)
# Node types that open their own variable scope; local-variable inference must
# not descend, or a name in a lambda / nested function / local class body gets
# attributed to the enclosing function's scope.
CPP_NESTED_SCOPE_NODE_TYPES = frozenset(
    (
        TS_CPP_FUNCTION_DEFINITION,
        TS_CPP_LAMBDA_EXPRESSION,
        *CPP_COMPOUND_TYPES,
    )
)

# Preprocessor conditional directive heads, matched at line start (C allows
# whitespace around '#'). Drives the whole-file-ERROR parse recovery: a
# conditional branch whose brace count does not balance (nlohmann's
# `#ifdef __cpp_lib_byteswap ... else { #endif`) breaks tree-sitter, which
# keeps every branch's tokens.
CPP_PREPROC_CONDITIONAL_PATTERN = (
    rb"^\s*#\s*(if|ifdef|ifndef|elif|elifdef|elifndef|else|endif)\b"
)
CPP_PREPROC_OPEN_DIRECTIVES = frozenset({b"if", b"ifdef", b"ifndef"})
CPP_PREPROC_SPLIT_DIRECTIVES = frozenset({b"elif", b"elifdef", b"elifndef", b"else"})

# Reserved keywords that error recovery can leave in declarator position
# (nlohmann: a macro access-label followed by `const decltype(MACRO_)`
# members parses as a function declaration NAMED decltype). None can ever
# name a real C/C++ function or method, so extraction rejects them.
CPP_RESERVED_DEF_NAMES = frozenset(
    {
        "decltype",
        "sizeof",
        "alignof",
        "alignas",
        "typeid",
        "static_assert",
        "noexcept",
        "typename",
        "template",
        "requires",
        "if",
        "for",
        "while",
        "switch",
        "return",
        "catch",
    }
)
